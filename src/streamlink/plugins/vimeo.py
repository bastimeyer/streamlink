"""
$description Global live-streaming and video hosting social platform.
$url vimeo.com
$type live, vod
$notes Password protected streams are not supported
"""

import logging
import re
from html import unescape as html_unescape
from time import time
from urllib.parse import urlparse

from streamlink.exceptions import StreamError
from streamlink.plugin import Plugin, PluginArgument, PluginArguments, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.dash import DASHStream
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.stream.hls import HLSStream, MuxedHLSStream
from streamlink.stream.http import HTTPStream

log = logging.getLogger(__name__)


class VimeoMuxedHLSStream(MuxedHLSStream):
    def __init__(self, session, video, audio, **args):
        super().__init__(session, video, audio, hlsstream=VimeoHLSStream, **args)


class VimeoHLSStream(HLSStream):
    __muxed__ = VimeoMuxedHLSStream

    def __init__(self, session_, url, **args):
        self.session = session_
        self._url = url
        self.api = args.pop("api")
        self._parsed_url = urlparse(self._url)
        self._path_parts = self._parsed_url.path.split("/")
        self._need_update = False
        self.api.set_expiry_time(self._path_parts[1])
        super().__init__(self.session, self._url, **args)

    @property
    def url(self):
        if self._need_update and not self.api.updating:
            self.api.set_expiry_time(self.api.auth_data)
            self._path_parts[1] = self.api.auth_data
            self._url = self._parsed_url._replace(path="/".join(self._path_parts)).geturl()
            log.debug("Reloaded Vimeo HLS URL")
            self._need_update = False

        time_now = time()
        if time_now > self.api.expiry_time:
            if self.api.updating or time_now - self.api.last_updated < VimeoAPI._expiry_time_limit:
                self._need_update = True
            else:
                log.debug("Reloading Vimeo auth data")
                self.api.reload_auth_data(self._path_parts[1])
                self._need_update = True

        return self._url


class VimeoAPI:
    _expiry_time_limit = 60
    _expiry_re1 = re.compile(r"^exp=(\d+)~")
    _expiry_re2 = re.compile(r"^(\d+)-")

    _config_schema = validate.Schema(
        validate.parse_json(),
        {
            "request": {
                "files": {
                    validate.optional("dash"): {"cdns": {str: {"url": validate.any(None, validate.url())}}},
                    validate.optional("hls"): {"cdns": {str: {"url": validate.any(None, validate.url())}}},
                    validate.optional("progressive"): validate.all(
                        [{"url": validate.url(), "quality": str}],
                    ),
                },
                validate.optional("text_tracks"): validate.all(
                    [{"url": str, "lang": str}],
                ),
            },
        },
    )

    def __init__(self, session, url):
        self.session = session
        self.url = url
        self.auth_data = None
        self.expiry_time = None
        self.last_updated = time()
        self.updating = False

    def get_player_data(self):
        return self.session.http.get(self.url, schema=validate.Schema(
            validate.transform(re.compile(r"var\s+config\s*=\s*({.+?})\s*;").search),
            validate.any(None, validate.Schema(validate.get(1), self._config_schema)),
        ))

    def get_api_url(self):
        return self.session.http.get(self.url, schema=validate.Schema(
            validate.transform(re.compile(r'(?:"config_url"|\bdata-config-url)\s*[:=]\s*(".+?")').search),
            validate.any(
                None,
                validate.Schema(
                    validate.get(1),
                    validate.parse_json(),
                    validate.transform(html_unescape),
                    validate.url(),
                ),
            ),
        ))

    def get_config_data(self, api_url):
        return self.session.http.get(api_url, schema=self._config_schema)

    def get_data(self):
        if "player.vimeo.com" in self.url:
            return self.get_player_data()
        else:
            api_url = self.get_api_url()
            if not api_url:
                return
            return self.get_config_data(api_url)

    def set_expiry_time(self, path):
        m = self._expiry_re1.search(path) or self._expiry_re2.search(path)
        if not m:
            raise StreamError("expiry value not found in URL")
        self.expiry_time = int(m.group(1)) - self._expiry_time_limit

    def reload_auth_data(self, auth_part):
        self.updating = True
        self.last_updated = time()

        data = self.get_data()
        if not data:
            raise StreamError("no video data found")
        videos = data["request"]["files"]
        if "hls" not in videos:
            raise StreamError("hls key not found in video data")

        new_auth = None
        for _, video_data in videos["hls"]["cdns"].items():
            url = video_data.get("url")
            res = self.session.http.get(url)
            if res.history:
                url = res.url

            parsed_url = urlparse(url)
            path_parts = parsed_url.path.split("/")
            if self._expiry_re1.search(path_parts[1]) and self._expiry_re1.search(auth_part):
                new_auth = path_parts[1]
            elif self._expiry_re2.search(path_parts[1]) and self._expiry_re2.search(auth_part):
                new_auth = path_parts[1]

            if new_auth:
                self.auth_data = new_auth
                break
        if not new_auth:
            raise StreamError("failed to get new auth data from URL")

        self.updating = False


@pluginmatcher(re.compile(
    r"https?://(player\.vimeo\.com/video/\d+|(www\.)?vimeo\.com/.+)"
))
class Vimeo(Plugin):
    arguments = PluginArguments(
        PluginArgument("mux-subtitles", is_global=True)
    )

    def _get_streams(self):
        self.api = VimeoAPI(self.session, self.url)
        data = self.api.get_data()
        if not data:
            return

        videos = data["request"]["files"]
        streams = []

        for stream_type in ("hls", "dash"):
            if stream_type not in videos:
                continue
            for _, video_data in videos[stream_type]["cdns"].items():
                log.trace("{0!r}".format(video_data))
                url = video_data.get("url")
                if not url:
                    log.error("This video requires a logged in session to view it")
                    return
                if stream_type == "hls":
                    for stream in VimeoHLSStream.parse_variant_playlist(self.session, url, api=self.api).items():
                        streams.append(stream)
                elif stream_type == "dash":
                    p = urlparse(url)
                    if p.path.endswith("dash.mpd"):
                        # LIVE
                        url = self.session.http.get(url).json()["url"]
                    elif p.path.endswith("master.json"):
                        # VOD
                        url = url.replace("master.json", "master.mpd")
                    else:
                        log.error("Unsupported DASH path: {0}".format(p.path))
                        continue

                    for stream in DASHStream.parse_manifest(self.session, url).items():
                        streams.append(stream)

        for stream in videos.get("progressive", []):
            streams.append((stream["quality"], HTTPStream(self.session, stream["url"])))

        if self.get_option("mux_subtitles") and data["request"].get("text_tracks"):
            substreams = {
                s["lang"]: HTTPStream(self.session, "https://vimeo.com" + s["url"])
                for s in data["request"]["text_tracks"]
            }
            for quality, stream in streams:
                yield quality, MuxedStream(self.session, stream, subtitles=substreams)
        else:
            for stream in streams:
                yield stream


__plugin__ = Vimeo
