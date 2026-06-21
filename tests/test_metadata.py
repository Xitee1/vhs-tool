from vhs_tool.metadata import build_global_tags_xml, parse_global_tags


def test_build_global_tags_xml_exact_format():
    # Byte-for-byte the format encode.py has always produced (mkvmerge --global-tags).
    xml = build_global_tags_xml([("SOURCE", "a"), ("COMMENT", "b")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Tags><Tag><Targets></Targets>"
        "<Simple><Name>SOURCE</Name><String>a</String></Simple>"
        "<Simple><Name>COMMENT</Name><String>b</String></Simple>"
        "</Tag></Tags>\n"
    )


def test_build_global_tags_xml_escapes_values():
    xml = build_global_tags_xml([("COMMENT", "Tom & Jerry <1998>")])
    assert "<String>Tom &amp; Jerry &lt;1998&gt;</String>" in xml


def test_parse_global_tags_skips_per_track_and_keeps_globals():
    # Mirrors `mkvextract tags` output: BOM, a global tag, and a per-track stats tag.
    xml = (
        '﻿<?xml version="1.0"?>\n'
        "<Tags>\n"
        "  <Tag><Targets/>"
        "<Simple><Name>SOURCE</Name><String>VCR</String></Simple>"
        "<Simple><Name>COMMENT</Name><String>hi</String></Simple></Tag>\n"
        "  <Tag><Targets><TrackUID>123</TrackUID></Targets>"
        "<Simple><Name>BPS</Name><String>15328</String></Simple></Tag>\n"
        "</Tags>\n"
    )
    assert parse_global_tags(xml) == {"SOURCE": "VCR", "COMMENT": "hi"}


def test_parse_global_tags_empty():
    assert parse_global_tags("") == {}
    assert parse_global_tags("﻿  \n") == {}


def test_roundtrip_and_merge():
    # PATCH flow: parse existing globals, override one, add one, rebuild, re-parse.
    existing = parse_global_tags(build_global_tags_xml([("SOURCE", "old"), ("COMMENT", "keep")]))
    existing["SOURCE"] = "new"  # override
    existing["DATE_RELEASED"] = "1998"  # add
    result = parse_global_tags(build_global_tags_xml(existing.items()))
    assert result == {"SOURCE": "new", "COMMENT": "keep", "DATE_RELEASED": "1998"}
