"""Unit tests for file system cache."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from mypy.fscache import FileSystemCache


class TestFileSystemCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp()
        self.oldcwd = os.getcwd()
        os.chdir(self.tempdir)
        self.fscache = FileSystemCache()

    def tearDown(self) -> None:
        os.chdir(self.oldcwd)
        shutil.rmtree(self.tempdir)

    def test_isfile_case_1(self) -> None:
        self.make_file("bar.py")
        self.make_file("pkg/sub_package/__init__.py")
        self.make_file("pkg/sub_package/foo.py")
        # Run twice to test both cached and non-cached code paths.
        for i in range(2):
            assert self.isfile_case("bar.py")
            assert self.isfile_case("pkg/sub_package/__init__.py")
            assert self.isfile_case("pkg/sub_package/foo.py")
            assert not self.isfile_case("non_existent.py")
            assert not self.isfile_case("pkg/non_existent.py")
            assert not self.isfile_case("pkg/")
            assert not self.isfile_case("bar.py/")
        for i in range(2):
            assert not self.isfile_case("Bar.py")
            assert not self.isfile_case("pkg/sub_package/__init__.PY")
            assert not self.isfile_case("pkg/Sub_Package/foo.py")
            assert not self.isfile_case("Pkg/sub_package/foo.py")

    def test_isfile_case_2(self) -> None:
        self.make_file("bar.py")
        self.make_file("pkg/sub_package/__init__.py")
        self.make_file("pkg/sub_package/foo.py")
        # Run twice to test both cached and non-cached code paths.
        # This reverses the order of checks from test_isfile_case_1.
        for i in range(2):
            assert not self.isfile_case("Bar.py")
            assert not self.isfile_case("pkg/sub_package/__init__.PY")
            assert not self.isfile_case("pkg/Sub_Package/foo.py")
            assert not self.isfile_case("Pkg/sub_package/foo.py")
        for i in range(2):
            assert self.isfile_case("bar.py")
            assert self.isfile_case("pkg/sub_package/__init__.py")
            assert self.isfile_case("pkg/sub_package/foo.py")
            assert not self.isfile_case("non_existent.py")
            assert not self.isfile_case("pkg/non_existent.py")

    def test_isfile_case_3(self) -> None:
        self.make_file("bar.py")
        self.make_file("pkg/sub_package/__init__.py")
        self.make_file("pkg/sub_package/foo.py")
        # Run twice to test both cached and non-cached code paths.
        for i in range(2):
            assert self.isfile_case("bar.py")
            assert not self.isfile_case("non_existent.py")
            assert not self.isfile_case("pkg/non_existent.py")
            assert not self.isfile_case("Bar.py")
            assert not self.isfile_case("pkg/sub_package/__init__.PY")
            assert not self.isfile_case("pkg/Sub_Package/foo.py")
            assert not self.isfile_case("Pkg/sub_package/foo.py")
            assert self.isfile_case("pkg/sub_package/__init__.py")
            assert self.isfile_case("pkg/sub_package/foo.py")

    def test_isfile_case_other_directory(self) -> None:
        self.make_file("bar.py")
        with tempfile.TemporaryDirectory() as other:
            self.make_file("other_dir.py", base=other)
            self.make_file("pkg/other_dir.py", base=other)
            assert self.isfile_case(os.path.join(other, "other_dir.py"))
            assert not self.isfile_case(os.path.join(other, "Other_Dir.py"))
            assert not self.isfile_case(os.path.join(other, "bar.py"))
            if os.path.exists(os.path.join(other, "PKG/other_dir.py")):
                # We only check case for directories under our prefix, and since
                # this path is not under the prefix, case difference is fine.
                assert self.isfile_case(os.path.join(other, "PKG/other_dir.py"))

    def make_file(self, path: str, base: str | None = None) -> None:
        if base is None:
            base = self.tempdir
        fullpath = os.path.join(base, path)
        os.makedirs(os.path.dirname(fullpath), exist_ok=True)
        if not path.endswith("/"):
            with open(fullpath, "w") as f:
                f.write("# test file")

    def isfile_case(self, path: str) -> bool:
        return self.fscache.isfile_case(os.path.join(self.tempdir, path), self.tempdir)
