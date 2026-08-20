#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
commit_state.py
================

GitHub Actionsワークフロー（scrape.yml）の「Commit updated geojson/state」ステップ用。
（geoplot-milプロジェクトのcommit_state.pyをそのまま移植したもの。設計思想・
コメントも含めて踏襲している。）

state.json は "source:id" をキーにした独立レコードの集合でしかないので、Gitの
行単位マージに頼らず、"originの最新state.json" と "このランで新しく計算した
state" をPythonの辞書として素直に統合（dict.update）すれば、衝突しようがない
（同じキーを両方が書き換えていた場合だけ「新しい方を採用」という単純な規則で
必ず解決できる）。

satellite_imagery.geojson は state から毎回re-buildできるものなので、統合後の
stateから作り直すだけでよい。

流れ:
  1. まず素直に `git add / commit / push` を試す（衝突が起きていなければこれで
     終わり）。
  2. push が拒否されたら:
       a. git fetch origin <branch>
       b. `git show origin/<branch>:<out-dir>/state.json` でリモートの最新
          state.jsonを取得
       c. ローカルでこのランが書き出した state.json と辞書として統合
          （キーが重複していればローカル＝このランの結果を優先）
       d. 統合結果で state.json を上書きし、satellite_imagery.geojson も
          作り直す
       e. add / commit(--amend ではなく新規commit) / push を再試行
     を最大5回、乱数バックオフを挟みながら繰り返す。

使い方（scrape.yml から呼ばれる想定。単体でも動く）:
  python commit_state.py --out-dir satellite_out --branch main

前提:
  - satellite_scraper.py と同じフォルダに置く。
  - 呼び出す前に satellite_scraper.py --once の実行が終わっていて、
    <out-dir>/state.json がこのランの最新状態になっていること。
  - git のuser.name/user.emailは呼び出し元（ワークフロー側）で設定済みであること。
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


def fetch_remote_state(branch: str, rel_path: str):
    run(["git", "fetch", "origin", branch], check=True)
    result = subprocess.run(
        ["git", "show", f"origin/{branch}:{rel_path}"],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None  # リモートにまだ無い（初回コミット前など）
    return json.loads(result.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="satellite_out")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--commit-message", default="Auto-update satellite_imagery.geojson [skip ci]")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    state_path = out_dir / "state.json"
    geojson_path = out_dir / "satellite_imagery.geojson"
    rel_state_path = f"{args.out_dir}/state.json"

    if not state_path.exists():
        sys.exit(f"{state_path} が見つかりません。先に satellite_scraper.py --once を実行してください。")

    local_state = scraper.load_state(str(state_path))

    run(["git", "add", str(geojson_path), str(state_path)])
    if not git_diff_staged_has_changes():
        print("no changes to commit", file=sys.stderr)
        return

    run(["git", "commit", "-m", args.commit_message])

    if try_push(args.branch):
        print("push succeeded on first try", file=sys.stderr)
        return

    print("push rejected, switching to dict-level merge against origin...", file=sys.stderr)

    for attempt in range(1, args.max_retries + 1):
        remote_state = fetch_remote_state(args.branch, rel_state_path)

        # 先にorigin/<branch>へ強制的に同期する（作業ディレクトリのファイルも
        # origin側の状態に書き換わる＝このランで書き出したstate.json/geojsonは
        # ここで一旦消える）。マージ結果のファイル書き出しは、必ずこの後に行うこと。
        # 逆順にすると、書き出した直後にresetで消してしまうバグになる
        # （実際に発生した不具合: originの最新コミットがsatellite_outディレクトリ
        # ごと削除するものだった場合、reset後にディレクトリ自体が無くなり、
        # 直後のgit addが「そんなファイルは無い」で失敗して落ちていた）。
        run(["git", "reset", "--hard", f"origin/{args.branch}"])

        if remote_state is None:
            merged_state = local_state
        else:
            merged_state = dict(remote_state)
            merged_state.update(local_state)

        out_dir.mkdir(parents=True, exist_ok=True)  # resetでディレクトリごと消えている場合があるため
        scraper.save_state(str(state_path), merged_state)
        geojson = scraper.build_geojson(merged_state)
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f, ensure_ascii=False, indent=2)

        run(["git", "add", str(geojson_path), str(state_path)])
        if not git_diff_staged_has_changes():
            print("merged result is identical to origin -- nothing new to commit", file=sys.stderr)
            return
        run(["git", "commit", "-m", args.commit_message])

        if try_push(args.branch):
            print(f"push succeeded after merge (attempt {attempt}/{args.max_retries})", file=sys.stderr)
            return

        print(f"push still rejected, retrying merge ({attempt}/{args.max_retries})...", file=sys.stderr)
        time.sleep(random.randint(5, 15))

    sys.exit("push failed after retries (dict-level merge)")


if __name__ == "__main__":
    main()
