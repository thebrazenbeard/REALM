"""Hostile tests: live GitHub must bind real source, not a cached manifest."""
import copy
import io
import json
import unittest
from email.message import Message
from unittest.mock import patch

from research.pr_stack_live import (API, canonical_origin, assess_live,
                                    fetch_live_open_prs, has_next_page,
                                    validate_live_pr, verify_origin)



class _Response(io.BytesIO):
    def __init__(self, value, link="", page=1, final_url=None, status=200):
        super().__init__(json.dumps(value).encode("utf-8"))
        self.status = status
        self.headers = {"Link": link}
        self.final_url = (final_url if final_url is not None else
                          f"{API}?state=open&per_page=100&page={page}")

    def geturl(self):
        return self.final_url


class PRStackLiveTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = "a" * 40, "b" * 40
        self.snapshot = {"schema": "realm.pr_stack_gate.v1",
                         "release_authority": "HOLD_NOT_GRANTED",
                         "prs": [{"n": 61, "head_ref": "feature/parent",
                                  "head_sha": self.a, "base_ref": "main",
                                  "base_sha": self.b,
                                  "release": "DRAFT_UNMERGED_HOLD"}]}
        self.live = {61: {"number": 61, "state": "open", "draft": True,
                          "merged_at": None, "head": {"ref": "feature/parent",
                          "sha": self.a, "repo": {"full_name": "thebrazenbeard/REALM"}},
                          "base": {"ref": "main", "sha": self.b,
                          "repo": {"full_name": "thebrazenbeard/REALM"}}}}
    def test_origin_exact_https_only(self):
        self.assertTrue(canonical_origin("https://github.com/thebrazenbeard/REALM.git"))
        for attack in ("git@github.com:thebrazenbeard/REALM.git",
                       "https://evil.example/thebrazenbeard/REALM.git",
                       "https://github.com/thebrazenbeard/REALM.git?token=secret",
                       "https://username@github.com/thebrazenbeard/REALM.git",
                       "https://github.com/thebrazenbeard/OTHER.git",
                       "https://github.com/thebrazenbeard/REALM.git/extra"):
            with self.subTest(attack=attack):
                self.assertFalse(canonical_origin(attack))

    def test_live_exact_head_and_draft(self):
        result = assess_live(self.snapshot, self.live)
        self.assertEqual(result["pr_gate"], "PASS")
        self.assertEqual(result["release"], "HOLD_NO_OWNER_AUTHORITY")
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["sha"] = "f" * 40
        self.assertIn("LIVE_HEAD_MOVED", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["draft"] = False
        self.assertIn("NOT_OPEN_DRAFT", assess_live(self.snapshot, bad)["findings"])
    def test_missing_extra_and_foreign_repo_fail_closed(self):
        self.assertIn("LIVE_PR_MISSING:61", assess_live(self.snapshot, {})["findings"])
        bad = copy.deepcopy(self.live)
        bad[92] = copy.deepcopy(bad[61])
        bad[92]["number"] = 92
        self.assertIn("UNTRACKED_OPEN_PR:92", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["repo"]["full_name"] = "evil/REALM"
        self.assertIn("FOREIGN_PR_REPO:61", assess_live(self.snapshot, bad)["findings"])

    def test_moved_base_and_ref_fail_closed(self):
        bad = copy.deepcopy(self.live)
        bad[61]["base"]["sha"] = "f" * 40
        self.assertIn("LIVE_BASE_MOVED:61", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["ref"] = "other"
        self.assertIn("LIVE_HEAD_REF_MOVED:61", assess_live(self.snapshot, bad)["findings"])

    def test_broken_live_schema_rejected(self):
        with self.assertRaises(ValueError):
            validate_live_pr({"number": 61, "head": {}, "base": {}})
    def test_closed_or_merged_never_passes(self):
        for key, value in (("state", "closed"), ("merged_at", "2026-11-07T00:00:00Z")):
            bad = copy.deepcopy(self.live)
            bad[61][key] = value
            self.assertIn("NOT_OPEN_DRAFT", assess_live(self.snapshot, bad)["findings"])

    def test_old_unrelated_pr_outside_bounded_manifest(self):
        bad = copy.deepcopy(self.live)
        bad[40] = copy.deepcopy(bad[61])
        bad[40]["number"] = 40
        self.assertEqual(assess_live(self.snapshot, bad)["pr_gate"], "PASS")

    def test_verified_origin_blocks_fake_or_unreachable(self):
        with patch("research.pr_stack_live.git", return_value=(0, "https://evil.invalid/fake.git", "")):
            with self.assertRaises(RuntimeError):
                verify_origin("unused")
        with patch("research.pr_stack_live.git", return_value=(2, "", "origin missing")):
            with self.assertRaises(RuntimeError):
                verify_origin("unused")

    def test_short_page_link_next_must_be_fetched(self):
        # Reproduction for PR103's earlier false PASS with short 27-item page.
        later = copy.deepcopy(self.live[61])
        later["number"] = 92
        link = f'<{API}?state=open&per_page=100&page=2>; rel="next"'
        with patch("research.pr_stack_live.urlopen",
                   side_effect=[_Response([self.live[61]], link),
                                _Response([later], page=2)]) as lookup:
            observed = fetch_live_open_prs()
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(set(observed), {61, 92})
        self.assertIn("UNTRACKED_OPEN_PR:92",
                      assess_live(self.snapshot, observed)["findings"])

    def test_separate_link_headers_must_follow_hidden_next_page(self):
        # A complete-looking 27-PR manifest must not hide PR #92 on page 2.
        rows = []
        manifest = copy.deepcopy(self.snapshot)
        for number in range(61, 88):
            pr = copy.deepcopy(self.live[61])
            pr["number"] = number
            rows.append(pr)
            if number != 61:
                pr["head"]["ref"] = f"feature/parent-{number}"
                rec = copy.deepcopy(manifest["prs"][0])
                rec["n"] = number
                rec["head_ref"] = pr["head"]["ref"]
                manifest["prs"].append(rec)
        first = _Response(rows)
        headers = Message()
        headers.add_header("Link", f'<{API}?state=open&per_page=100&page=1>; rel="prev"')
        headers.add_header("Link", f'<{API}?state=open&per_page=100&page=2>; rel="next"')
        first.headers = headers
        later = copy.deepcopy(self.live[61])
        later["number"] = 92
        with patch("research.pr_stack_live.urlopen",
                   side_effect=[first, _Response([later], page=2)]) as lookup:
            observed = fetch_live_open_prs()
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(len(observed), 28)
        self.assertEqual(assess_live(manifest, observed)["pr_gate"], "HOLD")
        self.assertIn("UNTRACKED_OPEN_PR:92",
                      assess_live(manifest, observed)["findings"])

    def test_separate_conflicting_next_headers_rejected(self):
        for second_page in (2, 3):
            with self.subTest(second_page=second_page):
                first = _Response([self.live[61]])
                headers = Message()
                headers.add_header("Link", f'<{API}?state=open&per_page=100&page=2>; rel="next"')
                headers.add_header("Link", f'<{API}?state=open&per_page=100&page={second_page}>; rel="next"')
                first.headers = headers
                with patch("research.pr_stack_live.urlopen",
                           side_effect=[first, _Response([])]) as lookup:
                    with self.assertRaises(ValueError):
                        fetch_live_open_prs()
                self.assertEqual(lookup.call_count, 1)

    def test_malformed_or_cyclic_pagination_fail_closed(self):
        bad = (
            f'<{API}?state=open&per_page=100&page=1>; rel="next"',
            '<https://evil.invalid/pulls?state=open&per_page=100&page=2>; rel="next"',
            f'<{API}?state=open&per_page=100&page=3>; rel="next"',
            f'<{API}?state=open&per_page=100&page=2>; rel="next", '
            f'<{API}?state=open&per_page=100&page=2>; rel="next"',
            'not-a-link',
        )
        for header in bad:
            with self.subTest(header=header):
                with self.assertRaises(ValueError):
                    has_next_page(header, 1)

    def test_short_page_duplicate_across_pages_rejected(self):
        link = f'<{API}?state=open&per_page=100&page=2>; rel="next"'
        with patch("research.pr_stack_live.urlopen",
                   side_effect=[_Response([self.live[61]], link),
                                _Response([self.live[61]], page=2)]):
            with self.assertRaises(ValueError):
                fetch_live_open_prs()

    def test_redirected_effective_source_url_never_passes(self):
        canonical = f"{API}?state=open&per_page=100&page=1"
        for destination in (
            "https://evil.example/fake",
            "http://api.github.com/repos/thebrazenbeard/REALM/pulls",
            "https://api.github.com/repos/other/OTHER/pulls",
            f"{API}?state=open&per_page=100&page=2",
            canonical + "&unexpected=1",
            "",
        ):
            with self.subTest(destination=destination):
                fake = _Response([self.live[61]], final_url=destination)
                with patch("research.pr_stack_live.urlopen", return_value=fake):
                    with self.assertRaises(RuntimeError):
                        fetch_live_open_prs()

    def test_redirect_handler_blocks_network_follow(self):
        from urllib.request import BaseHandler, Request, build_opener
        from urllib.response import addinfourl
        from research.pr_stack_live import _RefuseRedirect, urlopen

        class SyntheticTransport(BaseHandler):
            handler_order = 100

            def __init__(self, destination, code):
                self.destination = destination
                self.code = code
                self.requests = []
                self.responses = []

            def https_open(self, request):
                self.requests.append(request.full_url)
                headers = Message()
                headers.add_header("Location", self.destination)
                response = addinfourl(io.BytesIO(b""), headers, request.full_url,
                                      code=self.code)
                response.msg = "Found"
                self.responses.append(response)
                return response

            def http_open(self, request):
                self.requests.append(request.full_url)
                raise AssertionError("redirect reached foreign HTTP target")

        canonical = f"{API}?state=open&per_page=100&page=1"
        self.assertTrue(any(isinstance(handler, _RefuseRedirect)
                            for handler in urlopen.__self__.handlers))
        for destination in ("https://evil.example/steal",
                            "http://evil.example/steal",
                            f"{API}?state=open&per_page=100&page=2"):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(destination=destination, code=code):
                    transport = SyntheticTransport(destination, code)
                    opener = build_opener(_RefuseRedirect, transport)
                    try:
                        with self.assertRaisesRegex(RuntimeError, "redirect refused"):
                            opener.open(Request(canonical))
                        self.assertEqual(transport.requests, [canonical])
                    finally:
                        for response in transport.responses:
                            response.close()

    def test_second_page_effective_url_checked_and_http_errors_closed(self):
        link = f'<{API}?state=open&per_page=100&page=2>; rel="next"'
        later = copy.deepcopy(self.live[61])
        later["number"] = 92
        for redirected in ("http://evil.example/pulls", f"{API}?page=2"):
            with self.subTest(destination=redirected):
                with patch("research.pr_stack_live.urlopen",
                           side_effect=[_Response([self.live[61]], link),
                                        _Response([later], page=2, final_url=redirected)]) as lookup:
                    with self.assertRaises(RuntimeError):
                        fetch_live_open_prs()
                self.assertEqual(lookup.call_count, 2)
        with patch("research.pr_stack_live.urlopen",
                   return_value=_Response([self.live[61]], status=429)):
            with self.assertRaises(RuntimeError):
                fetch_live_open_prs()
        incomplete = _Response([self.live[61]])
        incomplete.seek(0)
        incomplete.truncate(1)
        incomplete.seek(0)
        with patch("research.pr_stack_live.urlopen", return_value=incomplete):
            with self.assertRaises(json.JSONDecodeError):
                fetch_live_open_prs()

    def test_full_page_must_probe_next_page_and_noncanonical_api_refused(self):
        rows = []
        for n in range(61, 161):
            value = copy.deepcopy(self.live[61])
            value["number"] = n
            rows.append(value)
        with patch("research.pr_stack_live.urlopen",
                   side_effect=[_Response(rows), _Response([], page=2)]) as lookup:
            self.assertEqual(len(fetch_live_open_prs()), 100)
        self.assertEqual(lookup.call_count, 2)
        with self.assertRaises(ValueError):
            fetch_live_open_prs("https://evil.invalid/pulls")


if __name__ == "__main__":
    unittest.main()
