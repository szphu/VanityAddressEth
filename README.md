# VanityAddressMaker

Ethereum の vanity address を、BIP39 ニーモニックから探索するための Python スクリプトです。

`vanityAddr.py` はランダムにニーモニックを生成し、指定した derivation path の address index 範囲から Ethereum アドレスを導出します。導出したアドレスが、指定した先頭文字列と末尾文字列の両方に一致した時点で探索を停止し、ニーモニック・アドレス・path を表示します。

## Features

- Ethereum アドレスの先頭 prefix と末尾 suffix を同時に指定
- 複数候補をカンマ区切りで指定
- EIP-55 checksum 表示の大文字小文字を厳密一致
- 12語または24語の BIP39 ニーモニックを生成
- `m/44'/60'/0'/0/<index>` の index 範囲を指定
- multiprocessing による並列探索
- 概算の期待探索回数と進捗表示

## Requirements

- Python 3.10 以上推奨
- `bip-utils==2.12.1`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Usage

```powershell
python .\vanityAddr.py
```

起動後、対話形式で条件を入力します。

```text
Ethereum Vanity Mnemonic Generator
入力した大文字小文字は、EIP-55表示上の厳密一致として扱います。
例: ace,Ace,ACE と入力すると、その3種類だけを許可します。
使える文字は 0-9, a-f, A-F だけです。

先頭候補をカンマ区切りで入力してください:
末尾候補をカンマ区切りで入力してください:
ニーモニック語数を選んでください [12/24]:
index範囲を入力してください。空Enterでデフォルト、例: 1-50:
workers数を入力してください。空Enterでデフォルト:
passphrase:
```

探索を開始する前に条件のサマリーが表示されます。内容を確認して `y` または `yes` を入力すると探索が始まります。

## Pattern Matching

候補には Ethereum アドレス本体で使える hex 文字だけを指定できます。

- 使用可能文字: `0-9`, `a-f`, `A-F`
- 先頭の `0x` は入力しても自動で取り除かれます
- 日本語の読点 `、` もカンマとして扱われます
- 候補の長さは最大40文字です
- prefix と suffix の合計が40文字を超える組み合わせだけの場合はエラーになります

大文字小文字は EIP-55 表示に対して厳密一致します。

例:

```text
先頭候補: ace,Ace,ACE
末尾候補: cafe,Cafe,CAFE
```

この場合、先頭は `ace`, `Ace`, `ACE` のいずれか、末尾は `cafe`, `Cafe`, `CAFE` のいずれかに一致するアドレスだけが採用されます。`aCe` や `CAfe` は一致しません。

## Address Path

探索対象の derivation path は以下です。

```text
m/44'/60'/0'/0/<index>
```

index 範囲のデフォルトは `1-50` です。たとえば `1-50` の場合、1つのニーモニックにつき50個の Ethereum アドレスを確認します。

`0` を含めることもできますが、`0` は最初の Ethereum アカウントに相当するため、スクリプトは警告を表示します。

## Output

条件に一致するアドレスが見つかると、以下の情報が表示されます。

- elapsed
- mnemonics tried
- addresses tried
- address
- address index
- path
- mnemonic

復元に必要なのは、少なくとも以下です。

- mnemonic
- path
- address
- passphrase を使った場合は passphrase

## Security Notes

このスクリプトは実際に資産を管理できるニーモニックを生成します。取り扱いには注意してください。

- ニーモニックをスクリーンショット、クラウドメモ、チャット、クリップボード履歴に保存しない
- 可能ならオフライン環境で実行する
- 表示された mnemonic、path、address、passphrase は紙などのオフライン媒体に記録する
- passphrase を設定した場合、復元時にも完全に同じ passphrase が必要
- このスクリプトや依存ライブラリの安全性を確認してから実資産に使う

## Notes

探索時間は指定した pattern の長さ、大文字小文字の指定、index 範囲、CPU 性能、workers 数に大きく依存します。長い prefix/suffix や大文字小文字まで厳密に指定した pattern は、見つかるまで非常に長い時間がかかる可能性があります。

workers のデフォルトは論理 CPU 数の約60%です。PC の負荷を下げたい場合は小さい値を指定してください。
