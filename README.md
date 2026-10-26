# Biancafiori WordPress Upgrade Regression Test

`http://biancafiori.com`（および特設サブサイト・BASIC認証サイト）の WordPress アップグレード前にベースラインを記録し、アップグレード後に**「アップグレード前後で変化・退行がないか」**を検証するツールです。

---

## 1. テスト対象サイトと検証項目

### 対象サイト
1. **`http://biancafiori.com` 配下の全WordPressサイト**
   - `/` （メインサイト）
   - `/30_years_anniversary/` （30周年記念特設サイト・**BASIC認証あり**）
   - `/31_program_additional/` （第31回定演 Webパンフレット）
   - `/32_program_additional/` （第32回定演 Webパンフレット）
   - `/33_program_additional/` （第33回定演 Webパンフレット）
2. **団員専用サイト**
   - `http://biancafiori.ciao.jp/members/` （**BASIC認証あり**）

### アップグレード前後で比較・検証される項目
- **BASIC認証の保護＆ログイン疎通**
  - 未認証アクセス時に `401 Unauthorized` が返ること（`.htaccess` 消失防止）
  - 認証ありアクセス時に `200 OK` でログイン・配下ページ巡回できること
- **全ページのHTTPステータスコード・リダイレクト先URLの一致**
- **各ページの `<title>`・見出し（`<h1>`/`<h2>`）・本文テキスト・ページ内リンクの一致**
- **ページ内の全画像・CSS・JS・PDF・内部リンクのステータスコード一致**
- **PHPエラー（Fatal/Warning/Deprecated等）・WordPress重大エラー・テスト用URL（`test.biancafiori.com`）の混入がないこと**

---

## 2. 使い方

ターミナルから `$HOME/biancafiori_regression_test` で実行します。

### Step 1: アップグレード前にベースラインを記録する (`record`)

```bash
cd $HOME/biancafiori_regression_test
python3 regression_test.py record
```

- 実行するとターミナル上で `/30_years_anniversary/` と `members` サイトの **BASIC認証ユーザー名・パスワード（非表示入力）** を尋ねられます。
- コマンドライン引数で渡す場合：
  ```bash
  # 両方のBASIC認証サイトが同じID/パスワードの場合
  python3 regression_test.py record --auth "ユーザー名:パスワード"

  # 別々のID/パスワードを指定する場合
  python3 regression_test.py record --auth-30 "user1:pass1" --auth-members "user2:pass2"
  ```

---

### Step 2: アップグレード後に比較検証を実行する (`verify`)

```bash
cd $HOME/biancafiori_regression_test
python3 regression_test.py verify
```
（コマンドライン引数 `--auth "ユーザー名:パスワード"` 等も同様に使えます）

- アップグレード前後で変化がなければ **`判定結果: PASS (アップグレード前後の差分 0 件)`** となります。
- 変化があった場合は具体的な差分内容（どのURLの何がどう変わったか）がターミナルと `reports/report_verify_YYYYMMDD_HHMMSS.md` に出力されます。
