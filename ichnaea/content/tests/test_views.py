from calendar import timegm
from unittest import mock

import boto3
from everett.manager import config_override
from pyramid.testing import DummyRequest
from pyramid import testing
import pytest

from ichnaea.content.views import get_map_tiles_url, ContentViews
from ichnaea.models.content import Stat, StatKey
from ichnaea import util


class TestConfig(object):
    def test_get_map_tiles_url(self):
        url = get_map_tiles_url("http://127.0.0.1:9/static")
        assert url == "http://127.0.0.1:9/static/tiles/{z}/{x}/{y}.png"


@pytest.fixture(scope="function")
def views(redis, session):
    request = DummyRequest()
    with testing.testConfig(request=request) as config:
        config.include("pyramid_chameleon")
        setattr(request, "db_session", session)
        setattr(request.registry, "redis_client", redis)
        yield ContentViews(request)


class TestContentViews(object):
    def test_homepage(self, views):
        result = views.homepage_view()
        assert result["page_title"] == "Overview"
        assert result["map_image_url"] == "http://127.0.0.1:9/static/tiles/0/0/0@2x.png"
        assert result["map_image_base_url"] == (
            "https://a.tiles.mapbox.com/v4/mapbox.dark/"
            "0/0/0@2x.png?access_token=pk.123456"
        )

    def test_map(self, views):
        result = views.map_view()
        assert result["page_title"] == "Map"
        tiles_url = "http://127.0.0.1:9/static/tiles/{z}/{x}/{y}.png"
        assert result["map_tiles_url"] == tiles_url
        assert result["map_token"] == "pk.123456"
        assert result["map_enabled"] is True

    @config_override(MAPBOX_TOKEN="")
    def test_map_disabled_if_no_mapbox_token(self, views):
        result = views.map_view()
        assert result["map_enabled"] is False

    def test_stats(self, session, session_tracker, views):
        today = util.utcnow().date()
        stats = [
            Stat(key=StatKey.blue, time=today, value=2200000),
            Stat(key=StatKey.cell, time=today, value=2000000),
            Stat(key=StatKey.wifi, time=today, value=2000000),
            Stat(key=StatKey.unique_blue, time=today, value=1500000),
            Stat(key=StatKey.unique_cell, time=today, value=1000000),
            Stat(key=StatKey.unique_wifi, time=today, value=2000000),
        ]
        session.add_all(stats)
        session.commit()
        session_tracker(1)

        result = views.stats_view()
        session_tracker(2)

        assert result["page_title"] == "Statistics"
        assert result["metrics1"] == [
            {"name": "Bluetooth Networks", "value": "1.50"},
            {"name": "Bluetooth Observations", "value": "2.20"},
            {"name": "Wifi Networks", "value": "2.00"},
            {"name": "Wifi Observations", "value": "2.00"},
        ]
        assert result["metrics2"] == [
            {"name": "MLS Cells", "value": "1.00"},
            {"name": "MLS Cell Observations", "value": "2.00"},
        ]

        second_result = views.stats_view()
        assert second_result == result
        # no additional DB query was done
        session_tracker(2)


class TestFunctionalContent(object):
    def test_content(self, app, session_tracker, metricsmock):
        app.get("/", status=200)
        app.get("/apple-touch-icon-precomposed.png", status=200)
        app.get("/api", status=200)
        app.get("/contact", status=200)
        app.get("/favicon.ico", status=200)
        app.get("/map", status=200)
        app.get("/nobody-is-home", status=404)
        app.get("/optout", status=200)
        app.get("/privacy", status=200)
        app.get("/robots.txt", status=200)
        app.get("/static/css/images/icons-000000@2x.png", status=200)
        app.get("/terms", status=200)
        session_tracker(0)
        app.get("/stats/regions", status=200)
        session_tracker(1)
        app.get("/stats", status=200)
        session_tracker(8)
        assert metricsmock.has_record(
            "incr", "request", value=1, tags=["path:", "method:get", "status:200"]
        )
        assert metricsmock.has_record(
            "incr", "request", value=1, tags=["path:map", "method:get", "status:200"]
        )
        assert metricsmock.has_record(
            "timing", "request.timing", tags=["path:", "method:get"]
        )
        assert metricsmock.has_record(
            "timing", "request.timing", tags=["path:map", "method:get"]
        )

    @config_override(ASSET_BUCKET="bucket", ASSET_URL="http://127.0.0.1:9/foo")
    def test_downloads(self, app):
        mock_conn = mock.MagicMock(name="conn")
        mock_bucket = mock.MagicMock(name="bucket")
        mock_conn.return_value.Bucket.return_value = mock_bucket
        key_prefix = "export/MLS-"

        class MockKey(object):
            def __init__(self, key, size):
                self.key = key_prefix + key
                self.size = size

        mock_bucket.objects.filter.return_value = [
            MockKey("full-cell-export-2016-02-24T000000.csv.gz", 1024),
            MockKey("diff-cell-export-2016-02-26T110000.csv.gz", 1000),
            MockKey("diff-cell-export-2016-02-26T100000.csv.gz", 1000),
            MockKey("full-cell-export-2016-02-26T000000.csv.gz", 8192),
            MockKey("diff-cell-export-2016-02-26T120000.csv.gz", 1000),
        ]
        with mock.patch.object(boto3, "resource", mock_conn):
            result = app.get("/downloads", status=200)
            assert "0kB" not in result.text
            assert "1kB" in result.text
            assert "8kB" in result.text

        # calling the page again should use the cache
        with mock.patch.object(boto3, "resource", mock_conn):
            result = app.get("/downloads", status=200)
            assert "1kB" in result.text

        # The mock / S3 API was only called once
        assert len(mock_bucket.objects.filter.mock_calls) == 1

    def test_headers_html(self, app):
        response = app.get("/", status=200)
        assert "X-Content-Type-Options" in response.headers
        assert "Strict-Transport-Security" in response.headers
        hsts = response.headers["Strict-Transport-Security"]
        assert "max-age" in hsts
        assert "includeSubDomains" in hsts
        assert "X-Frame-Options" in response.headers

        assert "Content-Security-Policy" in response.headers
        csp = response.headers["Content-Security-Policy"]
        # make sure CSP_BASE interpolation worked
        assert "'self'" in csp

    def test_headers_json(self, app):
        response = app.get("/__version__", status=200)
        assert "X-Content-Type-Options" in response.headers
        assert "Strict-Transport-Security" in response.headers
        hsts = response.headers["Strict-Transport-Security"]
        assert "max-age" in hsts
        assert "includeSubDomains" in hsts

    def test_map_json(self, app):
        result = app.get("/map.json", status=200)
        assert result.json["tiles_url"] == "http://127.0.0.1:9/static/tiles/"

    def test_stats_blue_json(self, app, session):
        today = util.utcnow().date()
        first_of_month = timegm(today.replace(day=1).timetuple()) * 1000
        session.add(Stat(key=StatKey.unique_blue, time=today, value=2))
        session.commit()
        result = app.get("/stats_blue.json", status=200)
        assert result.json == {
            "series": [{"data": [[first_of_month, 2]], "title": "MLS Bluetooth"}]
        }
        second_result = app.get("/stats_blue.json", status=200)
        assert second_result.json == result.json

    def test_stats_cell_json(self, app, session):
        today = util.utcnow().date()
        first_of_month = timegm(today.replace(day=1).timetuple()) * 1000
        session.add(Stat(key=StatKey.unique_cell, time=today, value=2))
        session.commit()
        result = app.get("/stats_cell.json", status=200)
        assert result.json == {
            "series": [{"data": [[first_of_month, 2]], "title": "MLS Cells"}]
        }
        second_result = app.get("/stats_cell.json", status=200)
        assert second_result.json == result.json

    def test_stats_wifi_json(self, app, session):
        today = util.utcnow().date()
        first_of_month = timegm(today.replace(day=1).timetuple()) * 1000
        session.add(Stat(key=StatKey.unique_wifi, time=today, value=2))
        session.commit()
        result = app.get("/stats_wifi.json", status=200)
        assert result.json == {
            "series": [{"data": [[first_of_month, 2]], "title": "MLS WiFi"}]
        }
        second_result = app.get("/stats_wifi.json", status=200)
        assert second_result.json == result.json
