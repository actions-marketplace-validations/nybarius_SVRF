"""GitHub through the `gh` CLI, every call counted and every call naming its repository.

Complete paginated REST snapshots; no GraphQL budget or operation is consumed.
The rate budget read (`gh api rate_limit`) names no repository.
"""

from __future__ import annotations

import json
import re
import subprocess

from .errors import ReadFailed, classify_gh_failure


def gh_argv(args: list[str], repo: str) -> list[str]:
    """The `gh` command line, refused unless it names its repository (`--repo <repo>` or a
    `repos/<repo>/` path)."""
    bound = (("--repo" in args and args.index("--repo") + 1 < len(args) and args[args.index("--repo") + 1] == repo)
             or any(a.startswith(f"repos/{repo}/") or a == f"repos/{repo}" for a in args)
             or list(args) == ["api", "rate_limit"])
    if not bound:
        raise ValueError(f"gh call names no repository: {args}")
    return ["gh", *args]


class RealGitHub:
    SNAPSHOT_FIELDS = "number,title,body,isDraft,headRefName,headRefOid,baseRefName,labels,isCrossRepository"

    def __init__(self, repo: str, limit: int = 300):
        self.repo = repo
        self.limit = limit
        self.calls = {"graphql": 0, "rest": 0}

    def _gh(self, args: list[str], kind: str | None) -> str:
        argv = gh_argv(args, self.repo)
        if kind:
            self.calls[kind] += 1
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ReadFailed("GH_TIMEOUT:60") from exc
        except OSError as exc:
            raise ReadFailed("GH_UNAVAILABLE:" + type(exc).__name__) from exc
        if done.returncode != 0:
            classify_gh_failure(done.returncode, done.stderr or done.stdout)
        return done.stdout

    def rate_limit(self) -> dict:
        try:
            core = json.loads(self._gh(["api", "rate_limit"], None))["resources"]["core"]
            if any(type(core[k]) is not int or core[k] < 0 for k in ("remaining", "reset")):
                raise ValueError("invalid core budget")
            return {"core": {k: core[k] for k in ("remaining", "reset")}}
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadFailed("RATE_BUDGET_UNOBSERVED") from exc

    def snapshot(self) -> list[dict]:
        if type(self.limit) is not int or self.limit < 1:
            raise ReadFailed("SNAPSHOT_LIMIT_INVALID")
        rows, seen, page = [], set(), 1
        while True:
            try:
                values = json.loads(self._gh([
                    "api", f"repos/{self.repo}/pulls?state=open&per_page=100&page={page}"], "rest"))
                if type(values) is not list or len(values) > 100:
                    raise ValueError("invalid page")
                for value in values:
                    number = value["number"]
                    head, base = value["head"], value["base"]
                    if (type(number) is not int or number < 1 or number in seen
                            or type(value["draft"]) is not bool or value["state"] != "open"
                            or type(value["title"]) is not str
                            or (value["body"] is not None and type(value["body"]) is not str)
                            or type(value["labels"]) is not list
                            or any(type(label["name"]) is not str for label in value["labels"])
                            or any(type(v) is not str or not v for v in
                                   (head["ref"], head["sha"], base["ref"],
                                    head["repo"]["full_name"], base["repo"]["full_name"]))
                            or not re.fullmatch(r"[0-9a-f]{40}", head["sha"])):
                        raise ValueError("invalid or repeated source row")
                    seen.add(number)
                    rows.append({"number": number, "title": value["title"], "body": value["body"],
                                 "isDraft": value["draft"], "headRefName": head["ref"],
                                 "headRefOid": head["sha"], "baseRefName": base["ref"],
                                 "labels": value["labels"],
                                 "isCrossRepository": head["repo"]["full_name"] != base["repo"]["full_name"]})
                if len(rows) > self.limit:
                    raise ReadFailed("SNAPSHOT_LIMIT_EXCEEDED")
                if len(values) < 100:
                    return rows
                page += 1
            except (KeyError, TypeError, ValueError) as exc:
                raise ReadFailed("SNAPSHOT_UNOBSERVED") from exc

    def pull(self, number: int) -> dict:
        value = json.loads(self._gh(["api", f"repos/{self.repo}/pulls/{number}"], "rest"))
        return {"number": number, "head_sha": value["head"]["sha"], "head_ref": value["head"]["ref"],
                "mergeable": value.get("mergeable"), "state": value.get("state"), "draft": value.get("draft")}

    def merge_commit(self, number: int) -> str | None:
        """The commit GitHub made when this pull request was merged, or None (never
        merged, or merged some other way that recorded no such commit)."""
        value = json.loads(self._gh(["api", f"repos/{self.repo}/pulls/{number}"], "rest"))
        return value.get("merge_commit_sha") if value.get("merged") else None

    def pulls_with_head(self, branch: str) -> list[dict]:
        """Every pull request (any state) whose head is `branch`: a stacked PR's parent."""
        owner = self.repo.split("/")[0]
        value = json.loads(self._gh(["api", f"repos/{self.repo}/pulls?state=all&per_page=20&head={owner}:{branch}"],
                                    "rest"))
        return [{"number": v["number"], "state": v.get("state"), "merged_at": v.get("merged_at")} for v in value]

    def retarget(self, number: int, base: str) -> None:
        self._gh(["api", "-X", "PATCH", f"repos/{self.repo}/pulls/{number}", "-f", f"base={base}"], "rest")

    def ready(self, number: int) -> None:
        # GitHub exposes this transition through GraphQL only. Preserve the
        # missing effect capability; do not spend a prohibited transport budget.
        raise ReadFailed("DRAFT_TRANSITION_REQUIRES_GRAPHQL")

    def merge(self, number: int, sha: str) -> str:
        """Merge with a merge commit, pinned to `sha`: GitHub refuses if the head moved."""
        value = json.loads(self._gh(["api", "-X", "PUT", f"repos/{self.repo}/pulls/{number}/merge",
                                     "-f", "merge_method=merge", "-f", f"sha={sha}"], "rest"))
        if not value.get("merged") or not value.get("sha"):
            raise ReadFailed(f"MERGE_NOT_CONFIRMED:{value.get('message', '')[:120]}")
        return value["sha"]

    def comment(self, number: int, body: str) -> None:
        self._gh(["api", f"repos/{self.repo}/issues/{number}/comments", "-f", f"body={body}"], "rest")

    def list_comments(self, number: int) -> list[dict]:
        value = json.loads(self._gh(["api", f"repos/{self.repo}/issues/{number}/comments?per_page=100"], "rest"))
        return [{"id": v["id"], "body": v.get("body") or ""} for v in value]

    def update_comment(self, comment_id: int, body: str) -> None:
        self._gh(["api", "-X", "PATCH", f"repos/{self.repo}/issues/comments/{comment_id}",
                  "-f", f"body={body}"], "rest")

    def set_status(self, sha: str, state: str, description: str, context: str = "svrf",
                   target_url: str | None = None) -> None:
        args = ["api", f"repos/{self.repo}/statuses/{sha}", "-f", f"state={state}", "-f", f"context={context}",
                "-f", f"description={description}"]
        if target_url:
            args += ["-f", f"target_url={target_url}"]
        self._gh(args, "rest")

    def close(self, number: int) -> None:
        self._gh(["api", "-X", "PATCH", f"repos/{self.repo}/pulls/{number}", "-f", "state=closed"], "rest")

    def open_pr(self, head: str, base: str, title: str, body: str) -> int:
        value = json.loads(self._gh(["api", f"repos/{self.repo}/pulls", "-f", f"title={title}",
                                     "-f", f"head={head}", "-f", f"base={base}", "-f", f"body={body}"], "rest"))
        return int(value["number"])
