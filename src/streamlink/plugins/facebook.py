"""
$description Global live-streaming and video hosting social platform.
$url facebook.com
$type live, vod
"""

import logging
import re
from urllib.parse import urlencode

from streamlink.plugin import Plugin, PluginError, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.dash import DASHStream
from streamlink.stream.http import HTTPStream


log = logging.getLogger(__name__)


@pluginmatcher(re.compile(r"""
    https?://(?:www\.)?facebook
    (?:\.com|wkhpilnemxj7asaniu7vnjjbiltxjqhye3mhbshg7kx5tfyd\.onion)
    /[^/]+/(?:posts|videos)/(?P<video_id>\d+)
""", re.VERBOSE))
class Facebook(Plugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.set_option("ffmpeg-start-at-zero", True)

    def _find_stream(self, quality, stream_url):
        if ".mpd" in stream_url:
            return DASHStream.parse_manifest(self.session, stream_url)
        elif ".mp4" in stream_url:
            return {quality: HTTPStream(self.session, stream_url)}

    def _find_meta(self, root):
        try:
            schema = validate.Schema(
                validate.xml_xpath_string(".//head/meta[@property='og:video:url'][@content][1]/@content"),
                str,
                validate.url(),
            )
            stream_url = schema.validate(root)
        except PluginError:
            log.debug("No meta og:video:url")
            return

        return self._find_stream("vod", stream_url)

    # TODO: check validity of this method and fix/improve xpath query (see _find_manifest)
    def _find_src(self, root):
        try:
            re_src = re.compile(r"""(?P<quality>sd|hd)_src["']?\s*:\s*(?P<quote>["'])(?P<url>.+?)(?P=quote)""")
            schema = validate.Schema(
                validate.xml_xpath_string(".//script[contains(text(),'_src')][1]/text()"),
                str,
                validate.transform(re_src.search),
                validate.union((
                    validate.get("quality"),
                    validate.all(
                        validate.get("url"),
                        validate.transform(lambda url: f"{{\"url\":\"{url}\"}}"),
                        validate.parse_json(),
                        validate.get("url"),
                        validate.url()
                    )
                ))
            )
            quality, stream_url = schema.validate(root)
        except PluginError:
            log.debug("Non-dash/mp4 stream")
            return

        return self._find_stream(quality, stream_url)

    def _find_manifest(self, root):
        try:
            re_manifest = re.compile(r"""(?P<json>"dash_manifest"\s*:\s*".+?"),""")
            schema = validate.Schema(
                validate.xml_xpath_string(".//script[contains(text(),'\"dash_manifest\"')][1]/text()"),
                str,
                validate.transform(re_manifest.search),
                validate.get("json"),
                validate.transform(lambda json: f"{{{json}}}"),
                validate.parse_json(),
                validate.get("dash_manifest"),
                validate.startswith("<?xml")
            )
            manifest = schema.validate(root)
        except PluginError:
            log.debug("No dash_manifest")
            return

        # Ignore unsupported manifests until DASH SegmentBase support is implemented
        if "SegmentBase" in manifest:
            log.error("Skipped DASH manifest with SegmentBase streams")
            return

        return DASHStream.parse_manifest(self.session, manifest)

    def _parse_streams(self, root):
        return self._find_meta(root) \
            or self._find_src(root) \
            or self._find_manifest(root)

    # TODO: rewrite and replace this with proper validation schemas
    def _find_tahoe(self, root):
        from lxml import etree

        text = etree.canonicalize(root)

        _playlist_re = re.compile(r'''video:\[({url:".+?}])''')
        _plurl_re = re.compile(r'''url:"(.*?)"''')
        _pc_re = re.compile(r'''pkg_cohort["']\s*:\s*["'](.+?)["']''')
        _rev_re = re.compile(r'''client_revision["']\s*:\s*(\d+),''')
        _dtsg_re = re.compile(r'''DTSGInitialData["'],\s*\[],\s*{\s*["']token["']\s*:\s*["'](.+?)["']''')
        _DEFAULT_PC = "PHASED:DEFAULT"
        _DEFAULT_REV = 4681796
        _TAHOE_URL = "https://www.facebook.com/video/tahoe/async/{0}/?chain=true&isvideo=true&payloadtype=primary"

        # fallback on to playlist
        log.debug("Falling back to playlist regex")
        match = _playlist_re.search(text)
        playlist = match and match.group(1)
        if playlist:
            match = _plurl_re.search(playlist)
            if match:
                url = match.group(1)
                return {"sd": HTTPStream(self.session, url)}

        # fallback to tahoe player url
        log.debug("Falling back to tahoe player")
        video_id = self.match.group("video_id")
        url = _TAHOE_URL.format(video_id)
        data = {
            "__a": 1,
            "__pc": _DEFAULT_PC,
            "__rev": _DEFAULT_REV,
            "fb_dtsg": "",
        }
        match = _pc_re.search(text)
        if match:
            data["__pc"] = match.group(1)
        match = _rev_re.search(text)
        if match:
            data["__rev"] = match.group(1)
        match = _dtsg_re.search(text)
        if match:
            data["fb_dtsg"] = match.group(1)
        root = self.session.http.post(
            url,
            headers={
                "Accept-Language": "en-US",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data=urlencode(data).encode("ascii"),
            schema=validate.Schema(validate.parse_html())
        )

        return self._parse_streams(root)

    def _get_streams(self):
        root, canonical, self.title = self.session.http.get(
            self.url,
            headers={
                "Accept-Language": "en-US"
            },
            schema=validate.Schema(
                validate.parse_html(),
                validate.union((
                    validate.xml_find("."),
                    validate.xml_xpath_string(".//head/meta[@res='canonical'][1]/@href"),
                    validate.xml_xpath_string(".//head/meta[@property='og:title'][1]/@content"),
                ))
            )
        )
        if canonical == "https://www.facebook.com/login/" or "log in" in self.title.lower():
            log.error("This URL requires a login or may be accessible from a different IP address.")
            return

        return self._parse_streams(root) \
            or self._find_tahoe(root)


__plugin__ = Facebook
