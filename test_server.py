"""單元測試；設 SELPHY_LIVE=1 會另外對真機跑 Validate-Job（只驗證、不出紙）。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import server


class TestPure(unittest.TestCase):
    def test_parse_attrs(self):
        out = "        printer-state (enum) = idle\n        media-ready (keyword) = om_dsc-photo_89x119mm\n"
        a = server.parse_attrs(out)
        self.assertEqual(a["printer-state"], "idle")
        self.assertEqual(a["media-ready"], "om_dsc-photo_89x119mm")

    def test_job_args_validate(self):
        args = server.build_job_args("t.test", "p.jpg", "L", 2, True)
        self.assertIn("op=Validate-Job", args)
        self.assertIn("xdim=8900", args)
        self.assertIn("ydim=11900", args)
        self.assertIn("copies=2", args)
        self.assertNotIn("-f", args)  # 驗證不可附檔
        self.assertNotIn("FILE", server.job_test_text(True))
        self.assertIn("FILE $filename", server.job_test_text(False))

    def test_job_args_print(self):
        self.assertIn("op=Print-Job", server.build_job_args("t", "p", "postcard", 1, False))

    def test_bordered_margins(self):
        args = server.build_job_args("t", "p", "L", 1, False, bordered=True)
        self.assertIn("lr=250", args)
        self.assertIn("tb=340", args)
        self.assertIn("lr=0", server.build_job_args("t", "p", "L", 1, False))
        with self.assertRaises(ValueError):
            server.build_job_args("t", "p", "card", 1, False, bordered=True)

    def test_exif_date_inserted(self):
        import cpnp
        j = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        out = cpnp.add_exif_date(j, "2026:10:31 16:58:44")
        self.assertEqual(out[2:4], b"\xff\xe1")
        self.assertIn(b"Exif\0\0II*\0", out)
        self.assertEqual(out.count(b"2026:10:31 16:58:44\0"), 2)   # DateTime + DateTimeOriginal
        self.assertEqual(cpnp.add_exif_date(out, "2000:01:01 00:00:00"), out)  # 已有 EXIF 不重寫

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            server.build_job_args("t", "p", "A4", 1, True)
        with self.assertRaises(ValueError):
            server.build_job_args("t", "p", "L", 0, True)


@unittest.skipUnless(os.environ.get("SELPHY_LIVE") == "1", "需要真機")
class TestLive(unittest.TestCase):
    def test_status(self):
        s = server.printer_status()
        self.assertTrue(s["online"], s)

    def test_validate_job(self):
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d) / "x.jpg"
            # 用 sips 產生一張 L 尺寸 300dpi 的白圖
            src = Path(d) / "x.png"
            src.write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
                "53de0000000c4944415408d763f8ffff3f0005fe02fea7d6a0b00000000049454e44ae426082"))
            subprocess.run(["sips", "-s", "format", "jpeg", "-z", "1406", "1051", str(src), "--out", str(jp)],
                           capture_output=True, check=True)
            r = server.submit_job(jp.read_bytes(), "L", 1, validate_only=True)
        self.assertTrue(r["ok"], r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
