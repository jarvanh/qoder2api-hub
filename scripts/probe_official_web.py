"""Explicit, credential-safe live probe; never runs during the offline tests."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qoder_accounts import Account
import qoder_official_web as official


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts-dir", required=True)
    parser.add_argument("--account", help="Existing account UID; required if more than one matches")
    parser.add_argument("--realm", choices=("cn", "intl"), help="Limit selection to one account realm")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--query")
    action.add_argument("--url")
    args = parser.parse_args()
    accounts = []
    for name in os.listdir(args.accounts_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(args.accounts_dir, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if (isinstance(data, dict) and data.get("accessToken")
                and data.get("enabled", True)
                and (not args.account or data.get("uid") == args.account)):
            account = Account(data)
            if not args.realm or account.realm == args.realm:
                accounts.append(account)
    if len(accounts) != 1:
        parser.error("select exactly one enabled account with --account UID")
    if args.query is not None:
        result = official.search(accounts[0], args.query)
        summary = {"operation": "search", "realm": accounts[0].realm, "ok": result["ok"],
                   "results": len(result.get("results") or [])}
    else:
        result = official.fetch(accounts[0], args.url)
        summary = {"operation": "fetch", "realm": accounts[0].realm, "ok": result["ok"],
                   "characters": len(result.get("text") or "")}
    if not result["ok"]:
        summary["error"] = result.get("error")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
