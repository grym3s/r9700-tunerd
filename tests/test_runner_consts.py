"""The hardware runner must classify kernel lines exactly like the daemon."""
import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        name, path, loader=importlib.machinery.SourceFileLoader(name, str(path))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_kernel_patterns_match_daemon():
    rt = _load("rt_consts", ROOT / "r9700-tunerd")
    hw = _load("hw_consts", ROOT / "tests" / "hw" / "r9700-hwtest.py")
    assert hw.KERNEL_FATAL_RE.pattern == rt.KERNEL_FATAL.pattern
    assert hw.KERNEL_FATAL_RE.flags == rt.KERNEL_FATAL.flags
    assert hw.KERNEL_OD_NOISE_RE.pattern == rt.KERNEL_OD_NOISE.pattern
    assert hw.KERNEL_OD_NOISE_RE.flags == rt.KERNEL_OD_NOISE.flags
