#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
commit_state.py
================

GitHub Actionsワークフロー（scrape.yml）の「Commit updated geojson/state」ステップ用。
2026-08にシャーディング対応で改訂（旧: state.json/satellite_imagery.geojson 1本
ずつだったものを、satellite_out/shards/{YYYY-MM}_{copernicus|mgp}.json 単位に
分割。経緯は satellite_scraper.py のモジュールdocstring参照）。

各シャードは "source:id" をキーにした独立レコードの集合でしかないので、Gitの
行単位マージに頼らず、"originの最新シャード" と "このランで新しく計算した
シャード" をPythonの辞書として素直に統合（dict.update）すれば、衝突しようがない
（同じキーを両方が書き換えていた場合だけ「新しい方を採用」という単純な規則で
必ず解決できる）。対応するgeojsonはシャードのstateから毎回re-buildする。

このファイルは2つの用途を持つ:

  1. satellite_scraper.py からインポートされ、commit_and_push_shards() が
     チェックポイントのたびに呼ばれる（大規模バックフィル中でも、途中経過を
     こまめにpushして途中経過を失わないようにするため）。

  2. scrape.yml の最終ステップとして単体実行され、下記を行う:
       a. 旧形式（satellite_out/state.json 1本）が残っていれば、シャード形式へ
          一括移行する（migrate_legacy_state）。
       b. git status でコミットされていないシャード変更を検出し、
          commit_and_push_shards() で確実に反映する（satellite_scraper.pyの
          最終チェックポイントが何らかの理由で失敗・スキップされた場合の保険）。

使い方:
  python commit_state.py --out-dir satellite_out --branch main
"""

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

try:
    import satellite_scraper as scraper
except ImportError:
    sys.exit("satellite_scraper.py が同じフォルダに見つかりません。同じフォルダで実行してください。")


def run(cmd, check=True, capture=False):
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def git_diff_staged_has_changes() -> bool:
    result = subprocess.run(["git", "diff", "--staged", "--quiet"])
    return result.returncode != 0  # quiet mode: 0=no diff, 1=has diff


def try_push(branch: str) -> bool:
    result = subprocess.run(["git", "push", "origin", branch])
    return result.returncode == 0


def fetch_remote_json(branch: str, rel_path: str):
    run(["git", "fetch", "origin", branch], check=True)
    result = subprocess.run(
        ["git", "show", f"origin/{branch}:{rel_path}"],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None  # リモートにまだ無い（初回コミット前など）
    return json.loads(result.stdout)


def commit_and_push_shards(out_dir, branch, touched_keys, max_retries=5,
                            commit_message="Auto-update satellite shards [skip ci]"):
    """touched_keys（'2025-12_copernicus' 等のシャードキーの集合）に対応する
    state.json/geojsonファイルと manifest.json を add/commit/push する。
    push が拒否されたら、シャード単位でoriginとマージしてリトライする。

    戻り値: 成功したら True。失敗したら False（呼び出し側で次回持ち越し等の
    ハンドリングをする想定なので、ここでは sys.exit しない）。
    """
    out_dir = Path(out_dir)
    touched_keys = sorted(set(touched_keys))
    if not touched_keys:
        return True

    def staged_paths():
        paths = []
        for key in touched_keys:
            paths.append(str(scraper.shard_state_path(out_dir, key)))
            paths.append(str(scraper.shard_geojson_path(out_dir, key)))
        paths.append(str(out_dir / "manifest.json"))
        return paths

    scraper.rebuild_manifest(out_dir)
    run(["git", "add"] + staged_paths())
    if not git_diff_staged_has_changes():
        print(f"no changes to commit for shards: {touched_keys}", file=sys.stderr)
        return True

    run(["git", "commit", "-m", commit_message])

    if try_push(branch):
        print(f"push succeeded for shards {touched_keys}", file=sys.stderr)
        return True

    print(f"push rejected for shards {touched_keys}, switching to per-shard merge against origin...", file=sys.stderr)

    for attempt in range(1, max_retries + 1):
        # このランで書き出したシャードの中身を、resetで消える前に退避しておく。
        local_shard_states = {key: scraper.load_shard(out_dir, key) for key in touched_keys}

        run(["git", "fetch", "origin", branch])
        run(["git", "reset", "--hard", f"origin/{branch}"])
        out_dir.mkdir(parents=True, exist_ok=True)  # resetでディレクトリごと消えている場合があるため

        for key in touched_keys:
            rel_path = f"{out_dir}/{scraper.SHARDS_DIR_NAME}/{key}.json"
            remote_state = fetch_remote_json(branch, rel_path)
            if remote_state is None:
                merged = local_shard_states[key]
            else:
                merged = dict(remote_state)
                merged.update(local_shard_states[key])
            scraper.save_shard(out_dir, key, merged)
        scraper.rebuild_manifest(out_dir)

        run(["git", "add"] + staged_paths())
        if not git_diff_staged_has_changes():
            print("merged result is identical to origin -- nothing new to commit", file=sys.stderr)
            return True
        run(["git", "commit", "-m", commit_message])

        if try_push(branch):
            print(f"push succeeded after merge (attempt {attempt}/{max_retries})", file=sys.stderr)
            return True

        print(f"push still rejected, retrying merge ({attempt}/{max_retries})...", file=sys.stderr)
        time.sleep(random.randint(5, 15))

    print(f"push failed after {max_retries} retries for shards {touched_keys}", file=sys.stderr)
    return False


def migrate_legacy_state(out_dir):
    """旧形式（satellite_out/state.json, satellite_out/satellite_imagery.geojson
    の単一ファイル）が残っていれば、シャード形式へ変換する。存在しなければ
    何もしない（新規リポジトリ・移行済みリポジトリの両方で安全に呼べる）。
    戻り値: 移行を実行したシャードキーの集合（無ければ空集合）。
    """
    out_dir = Path(out_dir)
    legacy_state_path = out_dir / "state.json"
    legacy_geojson_path = out_dir / "satellite_imagery.geojson"
    if not legacy_state_path.exists():
        return set()

    print(f"旧形式の {legacy_state_path} を検出。シャード形式へ移行します...", file=sys.stderr)
    legacy_state = scraper.load_state(legacy_state_path)
    touched = set()
    shard_states = {}
    for key, rec in legacy_state.items():
        sk = scraper.shard_key(rec.get("source", ""), rec.get("datetime"))
        shard_states.setdefault(sk, {})[key] = rec
        touched.add(sk)

    for sk, state in shard_states.items():
        existing = scraper.load_shard(out_dir, sk)
        existing.update(state)
        scraper.save_shard(out_dir, sk, existing)

    run(["git", "rm", "-f", "--ignore-unmatch", str(legacy_state_path), str(legacy_geojson_path)])
    # git rm は追跡済みファイルにしか効かない（未追跡のまま残っているケースへの保険として、
    # ワーキングディレクトリからも明示的に削除しておく）。
    legacy_state_path.unlink(missing_ok=True)
    legacy_geojson_path.unlink(missing_ok=True)
    print(f"移行完了: {len(legacy_state)}件を{len(shard_states)}個のシャードへ振り分けました", file=sys.stderr)
    return touched


def discover_uncommitted_shard_keys(out_dir):
    """git status から、コミットされていないシャードファイルの変更を検出し、
    対応するシャードキーの集合を返す。satellite_scraper.py側のチェックポイント
    処理が何らかの理由で失敗・スキップされていた場合の保険として使う。"""
    out_dir = Path(out_dir)
    shards_dir = out_dir / scraper.SHARDS_DIR_NAME
    result = subprocess.run(
        ["git", "status", "--porcelain", str(shards_dir)],
        text=True, capture_output=True, check=True,
    )
    keys = set()
    for line in result.stdout.splitlines():
        # 例: " M satellite_out/shards/2026-08_copernicus.json"
        path_part = line[3:].strip()
        if not path_part:
            continue
        p = Path(path_part)
        if p.suffix in (".json", ".geojson"):
            keys.add(p.stem)
    return keys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="satellite_out")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--commit-message", default="Auto-update satellite shards [skip ci]")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)

    # 1) 旧形式が残っていれば移行する。
    migrated_keys = migrate_legacy_state(out_dir)

    # 2) git status から、コミットされていないシャード変更（satellite_scraper.py側の
    #    最終チェックポイントが何らかの理由で反映しきれなかった分）を検出する。
    uncommitted_keys = discover_uncommitted_shard_keys(out_dir)

    touched_keys = migrated_keys | uncommitted_keys
    if not touched_keys:
        print("コミットが必要なシャードの変更はありません", file=sys.stderr)
        return

    ok = commit_and_push_shards(out_dir, args.branch, touched_keys,
                                 max_retries=args.max_retries,
                                 commit_message=args.commit_message)
    if not ok:
        sys.exit(f"push failed after retries for shards: {sorted(touched_keys)}")


if __name__ == "__main__":
    main()
