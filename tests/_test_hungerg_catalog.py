"""HungerG event model is advertised only while the official event is active."""
import os, sys, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish
P, A = bootstrap()
import qoder_catalog as C
declare_suite(__file__, ["qoder_proxy.py", "qoder_catalog.py", "qoder_catalog_intl.json"])

intl = {m["key"]: m for m in C.models_for_realm("intl")}
cn = {m["key"]: m for m in C.models_for_realm("cn")}
check("HungerG is in the intl snapshot", "HungerG" in intl)
check("HungerG is not advertised for cn", "HungerG" not in cn)
entry = intl["HungerG"]
check("HungerG keeps free event pricing", entry["price_factor"] == 0 and entry["is_free"] is True)
check("HungerG thinking efforts match the event", [e for e in ("low", "medium", "high", "max")
      if e in entry["thinking_config"]["enabled"]["efforts"]] == ["low", "medium", "high", "max"])
check("HungerG resolves by key/display/lowercase", all(P.resolve_model_key(name, "intl") == "HungerG"
      for name in ("HungerG", "hungerg")))
check("HungerG is intl-exclusive for routing", P.exclusive_realm("HungerG") == "intl"
      and C.key_realm_span("HungerG") == ("intl",))
live = [("auto", dict(entry, key="auto", display_name="Auto"))]
with mock.patch.object(P.time, "time", return_value=1796054399):
    listed = [mid for mid, _ in P.merge_catalog(live, "intl")]
    check("active event survives a dynamic list that omits it", "HungerG" in listed)
    public = P.model_entry("HungerG", entry, "intl")
    check("public entry keeps free price and intl marker", public["id"] == "HungerG"
          and public["price_factor"] == 0 and public["realm_exclusive"] == "intl")
with mock.patch.object(P.time, "time", return_value=1796054400):
    listed = [mid for mid, _ in P.merge_catalog(live, "intl")]
    check("expired event does not override the dynamic list", "HungerG" not in listed)
with mock.patch.object(P.time, "time", return_value=1796054400):
    listed = [mid for mid, _ in P.merge_catalog(live + [("HungerG", entry)], "intl")]
    check("an upstream-listed model still shows after the event", "HungerG" in listed)
finish()
