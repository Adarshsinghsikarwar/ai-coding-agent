"""
verify_setup.py
----------------
A quick, zero-API-cost sanity check. It does NOT call any LLM - it only
exercises RepoTools directly, so you can confirm the tool layer (listing,
reading, searching, path-traversal sandboxing) works before you spend
API credits on a full agent run.

Usage:
    python verify_setup.py --repo ./note-app
"""

import argparse
from agent.tools import RepoTools


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="./note-app")
    args = parser.parse_args()

    tools = RepoTools(args.repo)

    print("1) list_directory('.') ->")
    print(tools.list_directory("."))

    print("\n2) read_file('package.json') (first 5 lines) ->")
    print("\n".join(tools.read_file("package.json").splitlines()[:5]))

    print("\n3) search_files('mongoose.Schema') ->")
    print(tools.search_files("mongoose.Schema"))

    print("\n4) path-traversal guard (should raise PermissionError) ->")
    try:
        tools.read_file("../../etc/passwd")
        print("   FAILED: traversal was NOT blocked")
    except PermissionError as e:
        print(f"   OK, blocked: {e}")

    print("\nAll good. Tool layer is working. You can now run the full agent:")
    print('  python -m agent.main --repo ./note-app --request "..."')


if __name__ == "__main__":
    main()
