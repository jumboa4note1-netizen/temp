#!/usr/bin/env python3
"""
Parallel Batch Renderer & Hugging Face Dataset Sync Pipeline
=============================================================

Coordinates single-trigger GitHub Actions matrix workers to render every
MIDI file in `RdxradiTor/pianocore-subset-a-unique` into verified 16:10
1920x1200 lossless-piped H.264 MP4s and upload them to private HF dataset
`RdxradiTor/sus` (falling back automatically to `<owner>/sus-public` if
private access/upload is denied).

Modes:
  --mode prepare : enumerates source MIDIs, checks target HF repo for already
                   verified MP4s & failure logs, writes pending.json + CI outputs.
  --mode worker  : renders shard [shard_index::shard_total], verifies each MP4
                   with ffprobe, commits in batches to HF, stops cleanly at
                   --max-runtime-seconds (default 18000s = 5 hours).
  --mode trial   : picks ONE random MIDI longer than --min-duration seconds
                   (default 300 = 5 min), renders it and uploads it to
                   `trial/` in the PRIVATE target repo (never falls back to public).
  --mode merge   : merges worker reports, commits updated failure/status logs
                   to HF target repo, and reports remaining file count.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
from huggingface_hub.errors import HfHubHTTPError

MIN_VALID_MP4_BYTES = 8192
DEFAULT_SOURCE_REPO = "RdxradiTor/pianocore-subset-a-unique"
DEFAULT_TARGET_REPO = "RdxradiTor/sus"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def target_mp4_path(source_midi_path: str) -> str:
    """Map source repo MIDI path -> dataset video path `videos/<stem>.mp4`."""
    p = Path(source_midi_path)
    stem_rel = p.with_suffix(".mp4").as_posix().lstrip("/")
    return f"videos/{stem_rel}"


def is_midi_file(path: str) -> bool:
    low = path.lower()
    return low.endswith(".mid") or low.endswith(".midi")


def with_retries(fn: Callable[[], Any], *, attempts: int = 5, base_sleep: float = 2.0, label: str = "op") -> Any:
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i + 1 >= attempts:
                break
            wait = base_sleep * (2 ** i)
            print(f"[retry] {label} attempt {i + 1}/{attempts} failed ({exc}); sleeping {wait:.1f}s", file=sys.stderr, flush=True)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def resolve_target_dataset(api: HfApi, requested_repo: str) -> tuple[str, bool]:
    """
    Ensure we have a writable target HF dataset repo.
    Tries `requested_repo` (private first); if unauthorized / forbidden / unwritable,
    falls back cleanly to a public dataset (`<auth_user>/sus-public`).
    Returns `(repo_id, fallback_used)`.
    """
    who = with_retries(api.whoami, label="hf-whoami")
    username = str(who.get("name") or "").strip()
    if not username:
        raise RuntimeError("HF_TOKEN did not resolve to a valid Hugging Face account.")

    # Verify target repo exists & write capability by inspecting or creating private repo
    try:
        exists = False
        try:
            with_retries(lambda: api.repo_info(repo_id=requested_repo, repo_type="dataset"), label="check-target-repo", attempts=3)
            exists = True
        except HfHubHTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                exists = False
            else:
                raise
        if not exists:
            api.create_repo(repo_id=requested_repo, repo_type="dataset", private=True, exist_ok=True)
        # Smoke-test permission metadata write check? Wait: check user roles / auth or attempt read+create logs/.gitkeep once
        api.create_commit(
            repo_id=requested_repo,
            repo_type="dataset",
            operations=[],
            commit_message="Verify write access",
        )
        return requested_repo, False
    except Exception as priv_exc:
        print(f"[hf] Private target repo '{requested_repo}' unavailable/unwritable ({priv_exc}); switching to public fallback.", file=sys.stderr, flush=True)
        repo_name = requested_repo.split("/")[-1]
        public_repo = f"{username}/{repo_name}-public"
        with_retries(
            lambda: api.create_repo(repo_id=public_repo, repo_type="dataset", private=False, exist_ok=True),
            label="create-public-fallback",
        )
        return public_repo, True


def list_repo_files(api: HfApi, repo_id: str, token: Optional[str]) -> dict[str, int]:
    """Return `{path_in_repo: byte_size}` for every file in an HF dataset repo."""
    entries = with_retries(
        lambda: list(api.list_repo_tree(repo_id=repo_id, repo_type="dataset", recursive=True, token=token)),
        label=f"list-tree:{repo_id}",
    )
    out: dict[str, int] = {}
    for entry in entries:
        size = getattr(entry, "size", None)
        path = getattr(entry, "path", None)
        if path and size is not None:
            out[path] = int(size)
    return out


def download_json_if_exists(repo_id: str, filename: str, token: Optional[str]) -> Any:
    try:
        local = with_retries(
            lambda: hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=filename, token=token),
            label=f"fetch:{filename}",
            attempts=3,
        )
        return json.loads(Path(local).read_text(encoding="utf-8"))
    except Exception:
        return None


def compute_pending_items(
    source_files: dict[str, int],
    target_files: dict[str, int],
    previous_failures: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Pure planner logic:
    Returns `(pending_items, completed_source_paths)`.
    Previously failed files are placed first so retries run immediately.
    """
    all_midis = sorted(p for p in source_files if is_midi_file(p))
    completed: list[str] = []
    failed_pending: list[dict[str, Any]] = []
    fresh_pending: list[dict[str, Any]] = []

    for src in all_midis:
        dst = target_mp4_path(src)
        existing_size = target_files.get(dst, 0)
        if existing_size >= MIN_VALID_MP4_BYTES:
            completed.append(src)
            continue
        prev = previous_failures.get(src)
        item = {
            "source_path": src,
            "target_path": dst,
            "attempts": int(prev.get("attempts", 0)) if prev else 0,
            "last_error": prev.get("last_error") if prev else None,
        }
        if prev:
            failed_pending.append(item)
        else:
            fresh_pending.append(item)

    return failed_pending + fresh_pending, completed


def shard_items(items: list[dict[str, Any]], shard_index: int, shard_total: int) -> list[dict[str, Any]]:
    if shard_total <= 0:
        raise ValueError("shard_total must be positive")
    if shard_index < 0 or shard_index >= shard_total:
        raise ValueError("shard_index out of range")
    return items[shard_index::shard_total]


def set_github_outputs(pairs: dict[str, Any]) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as fh:
        for k, v in pairs.items():
            fh.write(f"{k}={v}\n")


# ---------------------------------------------------------------------- Modes
def cmd_prepare(args: argparse.Namespace) -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is required.")
    api = HfApi(token=token)

    source_files = list_repo_files(api, args.source_repo, token)
    all_midis = sorted(p for p in source_files if is_midi_file(p))
    target_repo, fallback_used = resolve_target_dataset(api, args.target_repo)
    target_files = list_repo_files(api, target_repo, token)

    failures_doc = download_json_if_exists(target_repo, "logs/failures.json", token) or {}
    prev_failures: dict[str, dict[str, Any]] = failures_doc.get("failures", {}) if isinstance(failures_doc, dict) else {}

    pending, completed = compute_pending_items(source_files, target_files, prev_failures)
    num_workers = min(max(1, args.max_workers), len(pending)) if pending else 0

    plan = {
        "generated_at": utc_now(),
        "source_repo": args.source_repo,
        "target_repo": target_repo,
        "fallback_used": fallback_used,
        "total_source_count": len(all_midis),
        "already_completed_count": len(completed),
        "pending_count": len(pending),
        "num_workers": num_workers,
        "items": pending,
        "previous_failures": prev_failures,
    }
    plan_path = Path(args.plan)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    matrix_list = list(range(num_workers))
    print(
        f"[prepare] source={args.source_repo} ({len(all_midis)} MIDIs) | target={target_repo} "
        f"(fallback={fallback_used}) | done={len(completed)} | pending={len(pending)} | workers={num_workers}",
        flush=True,
    )
    set_github_outputs(
        {
            "target_repo": target_repo,
            "fallback_used": str(fallback_used).lower(),
            "total_count": len(all_midis),
            "completed_count": len(completed),
            "pending_count": len(pending),
            "num_workers": num_workers,
            "has_work": str(num_workers > 0).lower(),
            "matrix": json.dumps(matrix_list),
        }
    )
    return 0


def _commit_stage_to_hf(
    api: HfApi,
    target_repo: str,
    staged: list[tuple[Path, str, dict[str, Any]]],
    shard_index: int,
    fallback_repo: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Commit staged local MP4s to HF dataset in one atomic batch. Returns active repo_id and committed records."""
    if not staged:
        return target_repo, []
    ops = [CommitOperationAdd(path_in_repo=dst, path_or_fileobj=str(local)) for local, dst, _ in staged]
    msg = f"Add {len(staged)} rendered 16:10 lossless-grade MP4s (shard {shard_index})"
    active_repo = target_repo
    try:
        with_retries(
            lambda: api.create_commit(repo_id=active_repo, repo_type="dataset", operations=ops, commit_message=msg),
            label=f"hf-commit-shard-{shard_index}",
            attempts=5,
        )
    except Exception as exc:
        if fallback_repo and fallback_repo != target_repo:
            print(f"[worker {shard_index}] private commit failed ({exc}); creating/using public repo {fallback_repo}", file=sys.stderr, flush=True)
            with_retries(
                lambda: api.create_repo(repo_id=fallback_repo, repo_type="dataset", private=False, exist_ok=True),
                label="create-public-midrun",
            )
            with_retries(
                lambda: api.create_commit(repo_id=fallback_repo, repo_type="dataset", operations=ops, commit_message=msg),
                label=f"hf-commit-public-shard-{shard_index}",
            )
            active_repo = fallback_repo
        else:
            raise

    committed_records = []
    for local, dst, rec in staged:
        committed_records.append(rec)
        try:
            if local.exists():
                local.unlink()
        except Exception:
            pass
    staged.clear()
    return active_repo, committed_records


def cmd_worker(args: argparse.Namespace) -> int:
    # Import FahhMidi lazily so prepare/merge/unit tests work without Playwright installed in merge job
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fahh_midi import FahhMidi

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is required.")
    api = HfApi(token=token)
    who = with_retries(api.whoami, label="hf-whoami")
    username = str(who.get("name") or "")

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    source_repo = str(plan["source_repo"])
    target_repo = str(plan["target_repo"])
    fallback_repo = f"{username}/{target_repo.split('/')[-1]}-public" if username else None
    items = shard_items(plan["items"], args.shard_index, args.shard_total)

    report: dict[str, Any] = {
        "shard_index": args.shard_index,
        "shard_total": args.shard_total,
        "started_at": utc_now(),
        "target_repo": target_repo,
        "assigned_count": len(items),
        "succeeded": [],
        "failed": [],
        "stopped_early": False,
        "stop_reason": None,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if not items:
        report["finished_at"] = utc_now()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[worker {args.shard_index}] empty shard, done.", flush=True)
        return 0

    stop_requested = False

    def _handle_sig(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        print(f"\n[worker {args.shard_index}] signal {signum} received, finishing safely...", file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    start_mono = time.monotonic()
    deadline = start_mono + max(60, args.max_runtime_seconds)
    safety_margin_s = 120.0  # leave room to flush HF commit before 5h mark

    staged: list[tuple[Path, str, dict[str, Any]]] = []
    work_dir = Path(tempfile.mkdtemp(prefix=f"fahh_worker_{args.shard_index}_"))

    fm: Optional[FahhMidi] = None
    try:
        def ensure_browser() -> FahhMidi:
            nonlocal fm
            if fm is None:
                fm = FahhMidi(timeout_s=120)
                fm.start()
            return fm

        for idx, item in enumerate(items):
            now = time.monotonic()
            remaining_s = deadline - now
            if stop_requested or remaining_s <= safety_margin_s:
                report["stopped_early"] = True
                report["stop_reason"] = "signal" if stop_requested else "max_runtime_reached"
                print(
                    f"[worker {args.shard_index}] stopping gracefully before timeout "
                    f"({idx}/{len(items)} processed, {remaining_s:.0f}s left).",
                    flush=True,
                )
                break

            src = item["source_path"]
            dst = item["target_path"]
            t0 = time.monotonic()
            local_mid: Optional[Path] = None
            local_mp4 = work_dir / f"render_{idx}.mp4"
            try:
                local_mid = Path(
                    with_retries(
                        lambda: hf_hub_download(
                            repo_id=source_repo,
                            repo_type="dataset",
                            filename=src,
                            token=token,
                            local_dir=str(work_dir / "dl"),
                        ),
                        label=f"dl:{src}",
                        attempts=4,
                    )
                )
                browser = ensure_browser()
                info = browser.load_midi(local_mid)
                res = browser.render_mp4_lossless(
                    local_mp4,
                    fps=args.fps,
                    width=args.width,
                    height=args.height,
                    crf=args.crf,
                    preset=args.preset_x264,
                    write_manifest=False,
                    log_interval_s=args.log_interval,
                    log_label=f"worker {args.shard_index} ({idx + 1}/{len(items)})",
                )
                probe = res["probe"]
                dt = time.monotonic() - t0
                rec = {
                    "source_path": src,
                    "target_path": dst,
                    "title": info.get("displayTitle"),
                    "key": info.get("key"),
                    "size_bytes": probe["size_bytes"],
                    "frames": probe["nb_frames"],
                    "duration_s": round(probe["duration"], 3),
                    "render_seconds": round(dt, 2),
                    "completed_at": utc_now(),
                }
                staged.append((local_mp4, dst, rec))
                print(
                    f"[worker {args.shard_index}] ({idx + 1}/{len(items)}) OK {src} -> "
                    f"{probe['size_bytes'] / (1024 * 1024):.2f}MB in {dt:.1f}s ({probe['nb_frames']}f)",
                    flush=True,
                )

                if len(staged) >= args.commit_batch:
                    target_repo, committed = _commit_stage_to_hf(api, target_repo, staged, args.shard_index, fallback_repo)
                    report["succeeded"].extend(committed)
                    report["target_repo"] = target_repo

            except Exception as exc:
                dt = time.monotonic() - t0
                err_msg = f"{type(exc).__name__}: {exc}"
                print(f"[worker {args.shard_index}] ({idx + 1}/{len(items)}) FAIL {src} after {dt:.1f}s: {err_msg}", file=sys.stderr, flush=True)
                report["failed"].append(
                    {
                        "source_path": src,
                        "target_path": dst,
                        "attempts": int(item.get("attempts", 0)) + 1,
                        "last_error": err_msg,
                        "last_attempt_utc": utc_now(),
                        "shard": args.shard_index,
                    }
                )
                if local_mp4.exists():
                    local_mp4.unlink()
                # Reset Chromium cleanly after any render failure
                try:
                    if fm:
                        fm.close()
                except Exception:
                    pass
                fm = None

            finally:
                if local_mid and local_mid.exists():
                    try:
                        local_mid.unlink()
                    except Exception:
                        pass

        # Flush final staged batch
        if staged:
            target_repo, committed = _commit_stage_to_hf(api, target_repo, staged, args.shard_index, fallback_repo)
            report["succeeded"].extend(committed)
            report["target_repo"] = target_repo

    finally:
        if fm:
            try:
                fm.close()
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)
        report["finished_at"] = utc_now()
        report["elapsed_seconds"] = round(time.monotonic() - start_mono, 2)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return 0


def cmd_trial(args: argparse.Namespace) -> int:
    """Single-file smoke test: random MIDI >= --min-duration seconds -> MP4 -> private HF repo `trial/`."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fahh_midi import FahhMidi

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is required.")
    api = HfApi(token=token)
    t_start = time.monotonic()

    # The trial must never leak to a public repo: create/verify the PRIVATE repo, no fallback.
    with_retries(
        lambda: api.create_repo(repo_id=args.target_repo, repo_type="dataset", private=True, exist_ok=True),
        label="trial-create-private-repo",
    )
    info_repo = with_retries(lambda: api.repo_info(repo_id=args.target_repo, repo_type="dataset"), label="trial-repo-info")
    if not getattr(info_repo, "private", True):
        raise SystemExit(f"Refusing to upload: {args.target_repo} is PUBLIC. Make it private first.")

    midis = sorted(p for p in list_repo_files(api, args.source_repo, token) if is_midi_file(p))
    if not midis:
        raise SystemExit(f"No MIDI files found in {args.source_repo}")
    rng = random.SystemRandom()
    rng.shuffle(midis)
    print(f"[trial] {len(midis)} MIDIs in source; searching for a random one >= {args.min_duration}s", flush=True)

    work_dir = Path(tempfile.mkdtemp(prefix="fahh_trial_"))
    fm: Optional[FahhMidi] = None
    try:
        fm = FahhMidi(timeout_s=120)
        fm.start()
        chosen: Optional[tuple[str, Path, dict[str, Any]]] = None
        tried = 0
        for src in midis[: args.max_candidates]:
            tried += 1
            try:
                local = Path(
                    with_retries(
                        lambda: hf_hub_download(
                            repo_id=args.source_repo, repo_type="dataset", filename=src,
                            token=token, local_dir=str(work_dir / "dl"),
                        ),
                        label=f"dl:{src}", attempts=3,
                    )
                )
                info = fm.load_midi(local)
            except Exception as exc:  # unreadable file: skip it
                print(f"[trial] skip {src}: {type(exc).__name__}: {exc}", flush=True)
                continue
            dur = float(info.get("durationSeconds") or 0)
            print(f"[trial] candidate {tried}: {src} = {dur:.0f}s", flush=True)
            if dur >= args.min_duration:
                chosen = (src, local, info)
                break
        if chosen is None:
            raise SystemExit(f"No MIDI >= {args.min_duration}s found in {tried} candidates.")

        src, _local, info = chosen
        dst = f"trial/{Path(target_mp4_path(src)).name}"
        out_mp4 = work_dir / "trial.mp4"
        print(f"[trial] RENDER {src} ({info['durationSeconds']:.0f}s, {info.get('noteCount')} notes)", flush=True)
        t_render = time.monotonic()
        res = fm.render_mp4_lossless(
            out_mp4, fps=args.fps, width=args.width, height=args.height,
            crf=args.crf, preset=args.preset_x264, write_manifest=False,
            log_interval_s=args.log_interval, log_label="trial",
        )
        render_s = time.monotonic() - t_render
        probe = res["probe"]
        if probe["size_bytes"] < MIN_VALID_MP4_BYTES:
            raise RuntimeError(f"Rendered MP4 is suspiciously small ({probe['size_bytes']} bytes)")

        stats = {
            "source_repo": args.source_repo,
            "source_path": src,
            "target_repo": args.target_repo,
            "target_path": dst,
            "title": info.get("displayTitle"),
            "midi_duration_s": round(float(info["durationSeconds"]), 2),
            "video_duration_s": round(probe["duration"], 2),
            "frames": probe["nb_frames"],
            "size_mb": round(probe["size_bytes"] / (1024 * 1024), 2),
            "render_seconds": round(render_s, 1),
            "render_fps": round(probe["nb_frames"] / render_s, 2),
            "realtime_ratio": round(render_s / max(1e-6, probe["duration"]), 2),
            "resolution": f"{args.width}x{args.height}@{args.fps}",
            "crf": args.crf,
            "preset": args.preset_x264,
            "completed_at": utc_now(),
        }
        stats_json = work_dir / "trial.json"
        stats_json.write_text(json.dumps(stats, indent=2), encoding="utf-8")

        t_up = time.monotonic()
        with_retries(
            lambda: api.create_commit(
                repo_id=args.target_repo, repo_type="dataset",
                operations=[
                    CommitOperationAdd(path_in_repo=dst, path_or_fileobj=str(out_mp4)),
                    CommitOperationAdd(path_in_repo=dst.replace(".mp4", ".json"), path_or_fileobj=str(stats_json)),
                ],
                commit_message=f"Trial render: {Path(src).name}",
            ),
            label="trial-commit", attempts=5,
        )
        stats["upload_seconds"] = round(time.monotonic() - t_up, 1)
        stats["total_seconds"] = round(time.monotonic() - t_start, 1)

        # Extrapolate to the full dataset (see README: estimates assume the same per-minute cost)
        per_video_min = stats["total_seconds"] / 60
        summary = [
            "## Fahh MIDI trial render",
            f"- **File:** `{src}` ({stats['midi_duration_s']:.0f}s MIDI)",
            f"- **Output:** `{args.target_repo}/{dst}` ({stats['size_mb']} MB, private)",
            f"- **Render:** {stats['render_seconds']}s for {stats['frames']} frames = {stats['render_fps']} fps ({stats['realtime_ratio']}x real-time)",
            f"- **Upload:** {stats['upload_seconds']}s | **Total:** {stats['total_seconds']}s ({per_video_min:.1f} min)",
        ]
        print("\n".join(summary), flush=True)
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as fh:
                fh.write("\n".join(summary) + "\n")
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    finally:
        try:
            if fm:
                fm.close()
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN environment variable is required.")
    api = HfApi(token=token)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    source_repo = str(plan["source_repo"])
    target_repo = str(plan["target_repo"])
    total_source_count = int(plan["total_source_count"])

    worker_files = sorted(Path(args.reports_dir).glob("**/worker_report_*.json"))
    all_succeeded: list[dict[str, Any]] = []
    merged_failures: dict[str, dict[str, Any]] = dict(plan.get("previous_failures") or {})
    stopped_early_any = False

    for wf in worker_files:
        rep = json.loads(wf.read_text(encoding="utf-8"))
        if rep.get("target_repo"):
            target_repo = rep["target_repo"]
        if rep.get("stopped_early"):
            stopped_early_any = True
        for ok in rep.get("succeeded", []):
            all_succeeded.append(ok)
            merged_failures.pop(ok["source_path"], None)
        for bad in rep.get("failed", []):
            merged_failures[bad["source_path"]] = bad

    # Ground-truth audit against target HF dataset tree
    source_files = list_repo_files(api, source_repo, token)
    target_files = list_repo_files(api, target_repo, token)
    pending_after, completed_after = compute_pending_items(source_files, target_files, merged_failures)
    for ok_src in completed_after:
        merged_failures.pop(ok_src, None)

    remaining = len(pending_after)
    verified_count = len(completed_after)

    status_doc = {
        "updated_at": utc_now(),
        "source_repo": source_repo,
        "target_repo": target_repo,
        "total_source_midis": total_source_count,
        "verified_mp4_count": verified_count,
        "remaining_count": remaining,
        "failed_count": len(merged_failures),
        "run_succeeded_count": len(all_succeeded),
        "stopped_early_for_cooldown": stopped_early_any,
        "complete": remaining == 0 and verified_count == total_source_count,
    }
    failures_doc = {
        "updated_at": utc_now(),
        "failed_count": len(merged_failures),
        "failures": merged_failures,
    }
    completed_doc = {
        "updated_at": utc_now(),
        "verified_count": verified_count,
        "completed_sources": completed_after,
    }

    tmp_dir = Path(tempfile.mkdtemp(prefix="fahh_merge_"))
    try:
        (tmp_dir / "batch_status.json").write_text(json.dumps(status_doc, indent=2), encoding="utf-8")
        (tmp_dir / "failures.json").write_text(json.dumps(failures_doc, indent=2), encoding="utf-8")
        (tmp_dir / "completed.json").write_text(json.dumps(completed_doc, indent=2), encoding="utf-8")
        ops = [
            CommitOperationAdd(path_in_repo="logs/batch_status.json", path_or_fileobj=str(tmp_dir / "batch_status.json")),
            CommitOperationAdd(path_in_repo="logs/failures.json", path_or_fileobj=str(tmp_dir / "failures.json")),
            CommitOperationAdd(path_in_repo="logs/completed.json", path_or_fileobj=str(tmp_dir / "completed.json")),
        ]
        with_retries(
            lambda: api.create_commit(
                repo_id=target_repo,
                repo_type="dataset",
                operations=ops,
                commit_message=f"Update batch logs: {verified_count}/{total_source_count} verified MP4s ({remaining} remaining)",
            ),
            label="commit-logs",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(
        f"[merge] verified={verified_count}/{total_source_count} | run_succeeded={len(all_succeeded)} | "
        f"remaining={remaining} | tracked_failures={len(merged_failures)} | complete={status_doc['complete']}",
        flush=True,
    )
    set_github_outputs(
        {
            "target_repo": target_repo,
            "verified_count": verified_count,
            "total_count": total_source_count,
            "remaining_count": remaining,
            "failed_count": len(merged_failures),
            "should_continue": str(remaining > 0).lower(),
        }
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fahh MIDI parallel HF batch render coordinator")
    ap.add_argument("--mode", required=True, choices=("prepare", "worker", "merge", "trial"))
    ap.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO)
    ap.add_argument("--target-repo", default=DEFAULT_TARGET_REPO)
    ap.add_argument("--plan", default="batch_state/pending.json")
    ap.add_argument("--max-workers", type=int, default=20)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--shard-total", type=int, default=1)
    ap.add_argument("--max-runtime-seconds", type=int, default=18000)
    ap.add_argument("--commit-batch", type=int, default=5)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--crf", type=int, default=10)
    ap.add_argument("--preset-x264", default="slow")
    ap.add_argument("--log-interval", type=float, default=float(os.environ.get("FAHH_LOG_INTERVAL_S", "300")), help="seconds between progress log lines")
    ap.add_argument("--min-duration", type=float, default=300.0, help="trial: minimum MIDI length in seconds")
    ap.add_argument("--max-candidates", type=int, default=80, help="trial: max random files to inspect")
    ap.add_argument("--report", default="batch_state/worker_report.json")
    ap.add_argument("--reports-dir", default="batch_state/reports")
    args = ap.parse_args(argv)

    if args.mode == "prepare":
        return cmd_prepare(args)
    if args.mode == "worker":
        return cmd_worker(args)
    if args.mode == "trial":
        return cmd_trial(args)
    return cmd_merge(args)


if __name__ == "__main__":
    raise SystemExit(main())
