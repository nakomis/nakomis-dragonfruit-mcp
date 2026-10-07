import json

import pytest

from nakomis_dragonfruit_mcp.printers import Printer, loader


@pytest.fixture
def drop_in(tmp_path, monkeypatch):
    directory = tmp_path / "drop-in"
    directory.mkdir()
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(directory))
    return directory


WEIRD = """
from nakomis_dragonfruit_mcp.printers import Printer

class MyWeirdPrinter(Printer):
    name = "myweirdprinter"
    preset_id = "elegoo-mars-5-ultra-ctb"
    def extra_slice_args(self, job):
        return ["--dither", "on"]
"""


def test_builtins_load():
    registry = loader.discover()
    assert {"mars5ultra", "athena8k"} <= registry.printers.keys()
    assert registry.failures == []
    assert registry.printers["mars5ultra"].route == "json"
    assert registry.printers["athena8k"].route == "py"


def test_py_drop_in_is_registered_by_name(drop_in):
    (drop_in / "my_weird_printer.py").write_text(WEIRD)
    registry = loader.discover()
    printer = registry.printers["myweirdprinter"]
    assert printer.route == "py"
    assert printer.extra_slice_args(None) == ["--dither", "on"]
    assert printer.source == drop_in / "my_weird_printer.py"


def test_lone_json_drop_in_is_named_by_file_stem(drop_in):
    (drop_in / "screen.json").write_text(
        json.dumps({"description": "my screen", "presetId": "x", "display": {}})
    )
    printer = loader.discover().printers["screen"]
    assert printer.route == "json"
    assert printer.description == "my screen"
    assert "description" not in printer.base_profile()


def test_name_clash_later_directory_wins_with_warning(drop_in):
    (drop_in / "mars5ultra.json").write_text(json.dumps({"presetId": "elegoo-mars-4-goo"}))
    registry = loader.discover()
    assert registry.printers["mars5ultra"].base_profile()["presetId"] == "elegoo-mars-4-goo"
    assert any("'mars5ultra'" in w and "replaces" in w for w in registry.warnings)


def test_broken_plugins_are_skipped_and_reported(drop_in):
    (drop_in / "syntax.py").write_text("class Oops(:\n")
    (drop_in / "raises.py").write_text("raise RuntimeError('boom')\n")
    (drop_in / "empty.py").write_text("x = 1\n")
    (drop_in / "noname.py").write_text(
        "from nakomis_dragonfruit_mcp.printers import Printer\nclass P(Printer): pass\n"
    )
    (drop_in / "noprofile.py").write_text(
        "from nakomis_dragonfruit_mcp.printers import Printer\nclass P(Printer):\n    name = 'p'\n"
    )
    (drop_in / "bad.json").write_text("{not json")
    (drop_in / "list.json").write_text("[]")
    (drop_in / "notaprofile.json").write_text("{}")
    (drop_in / "exits.py").write_text("raise SystemExit(3)\n")
    (drop_in / "_ignored.py").write_text("raise RuntimeError('never imported')\n")
    (drop_in / "good.py").write_text(WEIRD)
    registry = loader.discover()
    errors = {f.source.rsplit("/", 1)[1]: f.error for f in registry.failures}
    assert set(errors) == {
        "syntax.py",
        "raises.py",
        "empty.py",
        "noname.py",
        "noprofile.py",
        "bad.json",
        "list.json",
        "notaprofile.json",
        "exits.py",
    }
    assert "SyntaxError" in errors["syntax.py"]
    assert "boom" in errors["raises.py"]
    # The server still works: built-ins and the good plugin are all there.
    assert {"mars5ultra", "athena8k", "myweirdprinter"} <= registry.printers.keys()


def test_missing_directories_are_fine(monkeypatch, tmp_path):
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(tmp_path / "nope"))
    assert loader.discover().failures == []


def test_config_dir_printers_win_over_env_dir(drop_in):
    config_printers = loader.config_dir() / "printers"
    config_printers.mkdir(parents=True)
    (drop_in / "a.json").write_text(json.dumps({"presetId": "from-env"}))
    (config_printers / "a.json").write_text(json.dumps({"presetId": "from-config"}))
    registry = loader.discover()
    assert registry.printers["a"].base_profile()["presetId"] == "from-config"
    assert len(registry.warnings) == 1


def test_printer_choice_precedence(monkeypatch):
    assert loader.choose_name(None) == ("mars5ultra", "default")

    config = loader.config_dir()
    config.mkdir(parents=True)
    (config / "config.toml").write_text('printer = "from-config"\n')
    assert loader.choose_name(None) == ("from-config", "config.toml")

    monkeypatch.setenv("NDFM_PRINTER", "from-env")
    assert loader.choose_name(None) == ("from-env", "$NDFM_PRINTER")

    assert loader.choose_name("from-arg") == ("from-arg", "argument")


def test_bad_config_toml_is_an_error(monkeypatch):
    config = loader.config_dir()
    config.mkdir(parents=True)
    (config / "config.toml").write_text("printer = [")
    with pytest.raises(ValueError, match="config.toml"):
        loader.choose_name(None)


def test_unknown_printer_lists_what_exists_and_what_failed(drop_in):
    (drop_in / "broken.py").write_text("raise RuntimeError('boom')\n")
    with pytest.raises(ValueError, match=r"unknown printer 'nope'.*mars5ultra.*broken\.py"):
        loader.get(loader.discover(), "nope")


def test_format_swap_derives_a_custom_profile(fake_df):
    printer = loader.discover().printers["mars5ultra"]
    assert printer.effective_profile() == {"presetId": "elegoo-mars-5-ultra-ctb"}
    swapped = printer.effective_profile(".goo")
    assert "presetId" not in swapped
    assert swapped["display"]["outputFormat"] == ".goo"
    assert "formatVersion" not in swapped["display"]
    assert swapped["display"]["resolutionX"] == 8520
    # The preset itself is untouched.
    assert printer.effective_profile()["presetId"] == "elegoo-mars-5-ultra-ctb"


def test_format_swap_on_a_bundle(fake_df):
    bundle = {"version": 1, "printer": {"presetId": "elegoo-mars-5-ultra-ctb"}, "materials": []}
    printer = Printer("b", profile=bundle)
    swapped = printer.effective_profile(".goo")
    assert swapped["printer"]["display"]["outputFormat"] == ".goo"
    assert swapped["materials"] == []


def test_format_swap_of_unknown_preset_says_so(fake_df):
    with pytest.raises(ValueError, match="not found"):
        Printer("x", profile={"presetId": "no-such"}).effective_profile(".goo")


def test_build_volume_derived_from_screen(fake_df):
    printer = loader.discover().printers["mars5ultra"]
    assert printer.build_volume_mm() == {"width": 153.36, "depth": 77.76, "height": 165}
    assert printer.output_format() == ".ctb"
    assert printer.output_format(".goo") == ".goo"


def test_build_volume_unknown_preset_is_none(fake_df):
    printer = Printer("x", profile={"presetId": "no-such"})
    assert printer.build_volume_mm() is None
    assert printer.output_format() is None
