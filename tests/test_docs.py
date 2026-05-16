from pathlib import Path


def test_merge_docs_match_api_branch_delete_flow():
    root = Path(__file__).resolve().parents[1]
    docs = {
        "README.md": (root / "README.md").read_text(encoding="utf-8"),
        "GUIDE.md": (root / "GUIDE.md").read_text(encoding="utf-8"),
    }

    stale_fragment = "gh pr merge --rebase --delete-branch --match-head-commit"
    for path, text in docs.items():
        assert stale_fragment not in text, path

    assert "gh pr merge --rebase --match-head-commit <sha>" in docs["README.md"]
    assert "gh pr merge <pr> --rebase --match-head-commit <sha>" in docs["GUIDE.md"]
    assert "delete the PR head branch through the GitHub API" in docs["GUIDE.md"]
