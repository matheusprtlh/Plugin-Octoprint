"""Quick JS sanity: balance check and ensure ko/OctoPrint references resolve."""
import re
from pathlib import Path

src = Path("plugin/octoprint_marketplace_link/static/js/marketplace_link.js").read_text(encoding="utf-8")

# strip line comments
no_comments = re.sub(r"//[^\n]*", "", src)
# strip block comments
no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL)
# strip double-quoted strings
no_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', no_comments)
# strip single-quoted strings
no_strings = re.sub(r"'(?:\\.|[^'\\])*'", "''", no_strings)

assert no_strings.count("{") == no_strings.count("}"), (
    f"brace imbalance: {{={no_strings.count('{')} }}={no_strings.count('}')}"
)
assert no_strings.count("(") == no_strings.count(")"), (
    f"paren imbalance: (={no_strings.count('(')} )={no_strings.count(')')}"
)
assert no_strings.count("[") == no_strings.count("]"), (
    f"bracket imbalance: [={no_strings.count('[')} ]={no_strings.count(']')}"
)

# referenced view-model identifiers (sanity: settingsViewModel, OCTOPRINT_VIEWMODELS, ko, OctoPrint)
for needle in ("OCTOPRINT_VIEWMODELS", "settingsViewModel", "ko.observable", "OctoPrint.simpleApi"):
    assert needle in src, f"missing reference: {needle}"

# functions registered as KO bindings must exist
for binding in ("openSettings", "testWebhook", "testBambu", "navbarTooltip", "navbarIconClass",
                "navbarStateClass", "deliveryLabel", "deliveryClass", "lastDeliveryAgo",
                "bambuStatusLabel", "bambuFirmware", "bambuLastError", "bambuTestResult",
                "testResult"):
    assert f"self.{binding}" in src, f"binding {binding} not declared on self"

print("JS sanity: OK")
print(f"  total chars: {len(src)}")
print(f"  braces: {{={no_strings.count('{')} }}={no_strings.count('}')}")
print(f"  parens: (={no_strings.count('(')} )={no_strings.count(')')}")
