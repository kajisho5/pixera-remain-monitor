# SW PIXERA MONITOR v2.2.1

![PIXERA Monitor](docs/pixera-screenshot.png)

PIXERA（two / one / mini）を LAN 経由で監視する読み取り専用モニター。
再生状態・Remain（残り）・タイムライン全体（クリップ／キュー）をブラウザに表示する。

---

## 1. PIXERA 側の設定（1回だけ）

1. PIXERA の **Settings › API** サブタブを開く
2. 空きのアクセスポートを **JSON/TCP (dl)** に設定し、ポート番号を割り当てる
   - PIXERA の API ポートに固定の初期値は無い（0 にすると無効）。公式ドキュメント／サンプルは **1400**（2本目 1401）、
     旧バージョンでは 1412 が入っていた例もある。実機の API タブの値を確認するのが確実
   - 不明ならモニターの「LANを検索」が 1400/1401/1402/1403/1410/1411/1412 を総当たりする（約10秒）
3. **PIXERA を再起動**（このタブの変更は再起動で反映される）

※ 複数クライアントが同じポートに接続できるので、既存の制御系と併用可。
※ 本ツールは取得系コマンドしか投げない（Play/Stop 等は一切送らない）。

## 2. 起動（Windows）

Python 3.8+ が入っていれば追加インストール不要（標準ライブラリのみ）。
未導入なら python.org のインストーラで「Add python.exe to PATH」にチェックして入れる。

**`SW-PIXERA-MONITOR.bat` をダブルクリック** → 設定ウィンドウが開く。

- `PIXERA IP` に IP を入れて **接続してモニター起動**（カンマ区切りで複数台を同時監視: `192.168.0.220, 192.168.0.221:1401`） → ブラウザにモニター画面が開く
- NIC が複数ある場合は「このPCのNIC」で有線LAN側のIPを選ぶ（既定は自動）
- IP が分からなければ **LANを検索**（同一サブネットの候補ポート7種を総当たりし `IP:ポート` 形式で列挙。選ぶとIP/ポート両方が入る）
- 入力した IP/ポートは自動保存され、次回起動時は自動で接続する
- ウィンドウには接続状態・再生中タイムライン・REMAIN と、他端末用のURLが出る

実機なしの動作確認は `SW-PIXERA-MONITOR-DEMO.bat`（ダミーPIXERA内蔵）。

### 他のソフト（Companion 等）からは繋がっているのに見つからないとき
その接続設定に入っている IP とポートが答えなので、そのまま使うのが最速。
`SW-PIXERA-TEST.bat` をダブルクリック → `192.168.0.10:1400` の形で入力すると、

- TCP が届くか（届かなければ経路／セグメント／ファイアウォールの問題）
- PIXERA API が応答するか、その**通信方式**（JSON/TCP(dl) / pxr1ヘッダ / HTTP）
- 届かない場合は各NICを送信元に指定して再試行し、PIXERA固有ポート(27101/8020/1338/30001)にも届くか確認して、
  「機体には届くが1400だけ塞がれている」のか「そもそも届かない」のかを切り分ける

を判定する。v1.4.0 から 3方式すべてに自動対応するので、
Companion が使っているポートがどのモードでも、そのまま入力欄に入れれば繋がる。

### LANから自動で見つけたいとき
`SW-PIXERA-SEARCH.bat` をダブルクリック（GUIの「LANを検索」と同じ処理をコンソールで実行）。

- このPCの**全NIC**のサブネットを検索する（AV用LANのようにゲートウェイの無いNICも対象）
- API候補ポートに加え、**PIXERA固有のポート**（Engine Manager 27101 / Engine Web 8020 / Control Web 1338 など）も見る。
  APIが無効でもPIXERA機自体は見つかるので、`△ PIXERAらしき機体 — APIポートが開いていません` と出たら
  **Settings › API の設定漏れ**（割当て後にPIXERA再起動が必要）
- 見つかったポートは実際に `getApiRevision` を投げて本物か確認してから表示する
- IPが分かっているのにポート不明なら `py sw_pixera_monitor.py --scan-host 192.168.0.10`
  （またはGUIの「このIPを詳しく」）で 1-65535 を全部調べる

### 開かない・GUIが出ないとき
`SW-PIXERA-MONITOR-DEBUG.bat` をダブルクリック。黒い窓に
Python の場所 / バージョン / tkinter の有無 / エラー内容が出たまま止まるので、その内容を見る。
- Python が見つからない → python.org から導入（Add to PATH にチェック）
- `tkinter : 利用不可` → Microsoft Store 版 Python の可能性。python.org 版を入れ直すか、
  そのままでもブラウザ画面の上部で IP 指定して使える
- 起動時エラーは `%TEMP%\sw_pixera_monitor_error.log` にも残る

コマンドラインで直接:

```
python sw_pixera_monitor.py                        # 設定ウィンドウ
python sw_pixera_monitor.py --host 192.168.0.10 --console   # ウィンドウ無しで即起動
python sw_pixera_monitor.py --demo
```

モニター画面は `http://localhost:8770`。
**同一 LAN の別端末（タブレット等）からは `http://<このPCのIP>:8770/` で同じ画面が見られる。**

exe にしたい場合（任意）: `pip install pyinstaller` 後に
`pyinstaller --onefile --noconsole --name SW-PIXERA-MONITOR sw_pixera_monitor.py`

## 2.5 Companion / Stream Deck に REMAIN を出す

設定ウィンドウの **Companion** 欄に Companion が動いている PC の IP を入れる（既定ポート 8000）。
コマンドなら `--companion 192.168.0.50`（頻度は `--companion-rate`、既定 4Hz）。

先に Companion 側で **Custom Variables** に同名の変数を作っておくこと（ページ最下部の
**Create custom variable** の variableName 欄。Create Collection ではない）。作られていない変数は無視される。

うまく行かないときは `SW-PIXERA-COMPANION-TEST.bat` で書き込みを1回だけ試せる。
TCP接続の可否・HTTPステータス・原因の切り分けが出る。

| 変数名 | 内容 |
|---|---|
| `pixera_remain` / `pixera_remain_f` | 残り時間 HH:MM:SS / HH:MM:SS:FF |
| `pixera_position` / `pixera_position_f` | 現在位置 |
| `pixera_state` | PLAYING / PAUSED / STOPPED |
| `pixera_timeline` | タイムライン名 |
| `pixera_clip` | 今出ている素材名 |
| `pixera_cue` / `pixera_cue_remain` | 次キュー名 / そこまでの残り |
| `pixera_end_at` | 終了予定の実時刻 |
| `pixera_connected` / `pixera_devices` | OK・NG / 接続台数 |

ボタンのテキストに `$(custom:pixera_remain)` と書けば Stream Deck に残り時間が出る。
複数台監視のときは `pixera1_remain` `pixera2_remain` … と機体ごとに分かれる。

## 3. 画面

| 表示 | 内容 |
|---|---|
| REMAIN | 選択タイムラインの残り時間 HH:MM:SS:FF を全桁同サイズで極大表示（残60秒で琥珀／10秒で赤） |
| POSITION | 現在位置 HH:MM:SS:FF |
| NEXT CUE | 次キュー名＋そこまでのカウントダウン |
| END AT | 終了予定の実時刻（再生中のみ） |
| STATE | PLAYING / PAUSED / STOPPED |
| NOW PLAYING | 各レイヤーで今出ている素材名＋サムネイル（0.25秒ごとに更新） |
| 下部バー | 全レイヤー合成のタイムライン（クリップにサムネイル、再生中のクリップは白枠で強調）＋レイヤー別レーン＋キュー位置＋再生ヘッド |
| 左リスト | 機体ごとにグループ分けした全タイムラインの状態と残り（クリックで切替）。ヘッダに接続台数 |

- `F` キー：全画面フォーカスモード（ヘッダ・リスト・バーを隠して数字だけ）
- `Esc`：解除
- 接続先は設定ウィンドウ／ブラウザ画面ヘッダのどちらからでも変更可（`%APPDATA%\SEVENTHWELL\sw_pixera_monitor.json` に保存）

## 4. 仕組み

- PIXERA Native API（JSON-RPC 2.0 / TCP / デリミタ `0xPX`）
- 5Hz で `Timeline.getCurrentTime` `getTransportMode` `Compound.getCurrentCountdownOfTimeline` を
  1回の書き込みにパイプラインして取得、id で突き合わせ
- 8秒ごとに構造スキャン（レイヤー・クリップ・キュー）→ 全体の尺を算出
- ポーリング間はブラウザ側で再生速度から補間（表示は 60fps でヌルヌル動く）
- `Clip.getTime()` の単位（frames / seconds）は `Compound.getClipDurationInSecondsWithIndex`
  と突き合わせて自動判定
- 切断時は自動再接続、画面には理由と対処を表示

## 5. 既知の制限

- タイムライン全体の尺は「最後のクリップ終端 or 最後のキュー」から算出（API に尺の直接取得が無いため）
- ジャンプキューでループするショーでは Remain は「タイムライン終端まで」の値になる。
  区間管理には NEXT CUE のカウントダウンを見る
- キュー／クリップの追加はスキャン間隔（8秒）で反映。即時なら「再スキャン」
- PIXERA の API には「クリップに割り当てられた素材」を返す関数が無い。本ツールは
  (1) クリップのラベル名とリソース名の一致 (2) 再生中に実測した対応 の2段構えで結びつけるため、
  ラベルが空のクリップのサムネイルは **一度再生された時点で** 表示されるようになる

参考: PIXERA Native API Introduction / pixera_api_comments_rev204.txt

---

MIT License / Copyright (c) 2026 SEVENTHWELL
