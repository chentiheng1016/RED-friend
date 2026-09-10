"""Tests for agent_core.daemons.telegram.media_detect — extracted from the
500+ line media-detection block in daemon_telegram.py.

Coverage focus: URL parsing edge cases (Chinese punctuation strip, UA
override extraction), domain detection (protected streaming, unsupported
sources, YouTube), tool routing (HLS vs YouTube audio vs default), and
chat-state URL memory TTL.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core.daemons.telegram import media_detect as md


class UrlExtractionTests(unittest.TestCase):
    def test_extract_strips_chinese_trailing_punctuation(self):
        urls = md._extract_media_urls("看 https://x.com/y。 接著")
        self.assertEqual(urls, ["https://x.com/y"])

    def test_extract_returns_empty_for_no_url(self):
        self.assertEqual(md._extract_media_urls(""), [])
        self.assertEqual(md._extract_media_urls("沒有連結"), [])

    def test_clean_url_token_cuts_at_verb_glued_to_url(self):
        # User pastes URL+verb with no space — strip the verb off.
        cleaned = md._clean_url_token("https://x.com/y幫我下載影片")
        self.assertEqual(cleaned, "https://x.com/y")

    def test_hls_manifest_detected_in_path_and_query(self):
        self.assertTrue(md._is_hls_manifest_url("https://x/y.m3u8"))
        self.assertTrue(md._is_hls_manifest_url("https://x/y?f=m3u8"))
        self.assertFalse(md._is_hls_manifest_url("https://x/y.mp4"))


class UserAgentOverrideTests(unittest.TestCase):
    def test_ua_with_colon_extracted(self):
        ua = md._extract_user_agent_override("User-Agent: Mozilla/5.0 Win")
        self.assertEqual(ua, "Mozilla/5.0 Win")

    def test_ua_with_equals_form(self):
        ua = md._extract_user_agent_override("UA=Mozilla/5.0")
        self.assertEqual(ua, "Mozilla/5.0")

    def test_ua_quoted_value_unquoted(self):
        ua = md._extract_user_agent_override('User-Agent: "Mozilla/5.0"')
        self.assertEqual(ua, "Mozilla/5.0")

    def test_ua_with_inline_verb_drops_verb(self):
        ua = md._extract_user_agent_override("UA: Mozilla 幫我 下載影片")
        self.assertEqual(ua, "Mozilla")

    def test_ua_multiline_input_parses_first_ua_line_only(self):
        # splitlines() consumes the \n before the regex runs, so the X-Bad
        # line doesn't get glued onto Mozilla — it's a separate (non-matching)
        # line. This is the intended defense against header injection: by the
        # time we return, the value contains only what was on the UA line.
        ua = md._extract_user_agent_override("UA: Mozilla\nX-Bad: 1")
        self.assertEqual(ua, "Mozilla")
        self.assertNotIn("\n", ua)


class VerbDetectionTests(unittest.TestCase):
    def test_chinese_verb_detected(self):
        self.assertTrue(md._has_media_download_verb("幫我下載影片"))

    def test_english_verb_detected(self):
        self.assertTrue(md._has_media_download_verb("please download video"))

    def test_audio_specific_verb_distinguished(self):
        self.assertTrue(md._has_audio_download_verb("存成mp3"))
        self.assertFalse(md._has_audio_download_verb("幫我下載影片"))  # video-only verb

    def test_no_verb_returns_false(self):
        self.assertFalse(md._has_media_download_verb("這是什麼"))


class DomainDetectionTests(unittest.TestCase):
    def test_youtube_variants(self):
        self.assertTrue(md._is_youtube_url("https://youtube.com/watch?v=x"))
        self.assertTrue(md._is_youtube_url("https://www.youtube.com/watch?v=x"))
        self.assertTrue(md._is_youtube_url("https://youtu.be/abc"))
        self.assertFalse(md._is_youtube_url("https://vimeo.com/x"))

    def test_host_matches_subdomain(self):
        self.assertTrue(md._host_matches_domain("www.foo.com", "foo.com"))
        self.assertTrue(md._host_matches_domain("foo.com", "foo.com"))
        self.assertFalse(md._host_matches_domain("notfoo.com", "foo.com"))

    def test_protected_provider_netflix(self):
        self.assertEqual(
            md._protected_media_provider("https://www.netflix.com/watch/123"),
            "Netflix",
        )

    def test_protected_provider_amazon_path_gated(self):
        # Amazon is protected ONLY on /gp/video paths, not amazon.com generally.
        self.assertEqual(
            md._protected_media_provider("https://amazon.com/gp/video/x"),
            "Prime Video",
        )
        self.assertEqual(md._protected_media_provider("https://amazon.com/dp/X"), "")

    def test_unsupported_jable(self):
        self.assertIn("piracy", md._unsupported_media_reason("https://jable.tv/x"))
        self.assertEqual(md._unsupported_media_reason("https://example.com"), "")


class ToolRoutingTests(unittest.TestCase):
    def test_hls_routes_to_n_m3u8dl_by_default(self):
        self.assertEqual(
            md._media_download_tool_for_url("https://x/y.m3u8"),
            "download_hls_with_n_m3u8dl",
        )

    def test_hls_with_ytdlp_hint_overrides(self):
        self.assertEqual(
            md._media_download_tool_for_url("https://x/y.m3u8", "用 yt-dlp 下載"),
            "download_hls_with_ytdlp",
        )

    def test_youtube_audio_verb_picks_audio_tool(self):
        self.assertEqual(
            md._media_download_tool_for_url(
                "https://youtu.be/abc", "存成mp3"
            ),
            "download_youtube_audio",
        )

    def test_default_video_tool(self):
        self.assertEqual(
            md._media_download_tool_for_url("https://instagram.com/p/abc"),
            "download_online_video",
        )

    def test_hls_args_include_user_agent_when_present(self):
        args = md._media_download_args_for_url(
            "https://x/y.m3u8", "UA: Mozilla/5.0"
        )
        self.assertEqual(args["manifest_url"], "https://x/y.m3u8")
        self.assertEqual(args["user_agent"], "Mozilla/5.0")

    def test_extract_direct_url_requires_both_url_and_verb(self):
        # URL without verb → empty (let the model handle it).
        self.assertEqual(
            md._extract_direct_media_download_url("https://youtube.com/watch?v=x"),
            "",
        )
        # Verb without URL → empty.
        self.assertEqual(md._extract_direct_media_download_url("幫我下載影片"), "")
        # Both → returns URL.
        self.assertEqual(
            md._extract_direct_media_download_url(
                "https://youtube.com/watch?v=x 幫我下載影片"
            ),
            "https://youtube.com/watch?v=x",
        )


class ChatStateUrlMemoryTests(unittest.TestCase):
    def test_remember_then_recall_within_ttl(self):
        state: dict = {}
        md._remember_media_url_in_chat(state, "看 https://youtube.com/watch?v=abc")
        self.assertEqual(
            md._recent_media_url_from_chat(state),
            "https://youtube.com/watch?v=abc",
        )

    def test_recall_expires_after_ttl(self):
        state: dict = {
            md._TG_LAST_MEDIA_URL_KEY: {
                "url": "https://youtube.com/x",
                "ts": time.time() - md._TG_PENDING_MEDIA_DOWNLOAD_TTL_S - 1,
            }
        }
        self.assertEqual(md._recent_media_url_from_chat(state), "")
        # Expired entry should be cleared off.
        self.assertNotIn(md._TG_LAST_MEDIA_URL_KEY, state)

    def test_remember_ignores_non_media_url(self):
        state: dict = {}
        md._remember_media_url_in_chat(state, "see https://example.com/doc")
        self.assertEqual(md._recent_media_url_from_chat(state), "")

    def test_remember_handles_none_state_silently(self):
        # Caller passes None when chat_state isn't set up yet — must not crash.
        md._remember_media_url_in_chat(None, "https://youtube.com/x")


if __name__ == "__main__":
    unittest.main()
