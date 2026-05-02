import multiprocessing as mp
import os
import queue
import secrets
import time
from dataclasses import dataclass

from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)


DEFAULT_INDEX_START = 1
DEFAULT_INDEX_END = 50
ETH_BASE_PATH = "m/44'/60'/0'/0"
HEX_CHARS = set("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class Pattern:
    """
    display:
      EIP-55表示として一致させたい文字列。
      例: "ace", "Ace", "ACE", "cafe", "CAFE"

    raw:
      大文字小文字を無視したhex文字列。
      例: "ACE" -> "ace"
    """
    display: str
    raw: str


# Windows multiprocessing で子プロセスへ渡すため、グローバル変数として保持する。
# spawn方式では各プロセス起動時にpickleされる。
PREFIX_PATTERNS = []
SUFFIX_PATTERNS = []
EXPECTED_ADDRESSES = None


def normalize_commas(text: str) -> str:
    """日本語の読点もカンマ扱いにする。"""
    return text.replace("、", ",")


def strip_optional_0x(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("0x"):
        return text[2:]
    return text


def parse_candidate_list(raw_input: str):
    """
    カンマ区切りの候補をPatternリストにする。

    例:
      ace,Ace,ACE ->
        Pattern("ace", "ace"), Pattern("Ace", "ace"), Pattern("ACE", "ace")

    入力した大文字小文字は、EIP-55表示上の厳密一致として扱う。
    """
    normalized = normalize_commas(raw_input)
    parts = [strip_optional_0x(p) for p in normalized.split(",")]
    parts = [p for p in parts if p]

    if not parts:
        raise ValueError("候補が空です。例: ace,Ace,ACE")

    seen = set()
    patterns = []

    for candidate in parts:
        if any(ch not in HEX_CHARS for ch in candidate):
            bad_chars = sorted({ch for ch in candidate if ch not in HEX_CHARS})
            raise ValueError(
                f"使用不可文字があります: {''.join(bad_chars)} / "
                "使えるのは 0-9, a-f, A-F だけです。"
            )

        if len(candidate) > 40:
            raise ValueError("候補が長すぎます。Ethereumアドレス本体は40文字です。")

        if candidate in seen:
            continue

        seen.add(candidate)
        patterns.append(Pattern(display=candidate, raw=candidate.lower()))

    return patterns


def ask_patterns(label: str):
    while True:
        print()
        text = input(f"{label}候補をカンマ区切りで入力してください: ").strip()

        try:
            patterns = parse_candidate_list(text)
        except ValueError as e:
            print(f"ERROR: {e}")
            print("もう一度入力してください。")
            continue

        print(f"{label}候補:")
        for p in patterns:
            print(f"  display={p.display}  raw={p.raw}")

        return patterns


def ask_words() -> int:
    while True:
        print()
        text = input("ニーモニック語数を選んでください [12/24]: ").strip()

        if text in {"12", "24"}:
            return int(text)

        print("ERROR: 12 または 24 を入力してください。")


def ask_index_range():
    """
    index範囲はデフォルト1〜50。
    何も入力しなければデフォルトを使う。
    """
    print()
    print(f"address index range: default {DEFAULT_INDEX_START}-{DEFAULT_INDEX_END}")

    while True:
        text = input("index範囲を入力してください。空Enterでデフォルト、例: 1-50: ").strip()

        if not text:
            return DEFAULT_INDEX_START, DEFAULT_INDEX_END

        text = text.replace("〜", "-").replace("–", "-").replace("—", "-")

        if "-" not in text:
            print("ERROR: 例のように 1-50 と入力してください。")
            continue

        left, right = [x.strip() for x in text.split("-", 1)]

        try:
            index_start = int(left)
            index_end = int(right)
        except ValueError:
            print("ERROR: indexは数値で入力してください。")
            continue

        if index_start < 0:
            print("ERROR: index_start must be >= 0")
            continue

        if index_end < index_start:
            print("ERROR: index_end must be >= index_start")
            continue

        return index_start, index_end


def ask_workers():
    print()
    logical_cpu = os.cpu_count() or 1
    default_workers = max(1, int(logical_cpu * 0.6))
    print(f"logical CPU: {logical_cpu}")
    print(f"workers default: {default_workers}  # 約60%目安")

    while True:
        text = input("workers数を入力してください。空Enterでデフォルト: ").strip()

        if not text:
            return default_workers

        try:
            workers = int(text)
        except ValueError:
            print("ERROR: workersは数値で入力してください。")
            continue

        if workers < 1:
            print("ERROR: workers must be >= 1")
            continue

        return workers


def ask_passphrase():
    print()
    print("BIP39 passphraseを使う場合のみ入力してください。")
    print("通常は空Enter推奨です。使うと復元時にも同じpassphraseが必須になります。")
    return input("passphrase: ")


def patterns_are_possible(prefix_patterns, suffix_patterns) -> bool:
    """prefix + suffix が40文字以内になる組み合わせが1つでもあるか。"""
    for prefix in prefix_patterns:
        for suffix in suffix_patterns:
            if len(prefix.raw) + len(suffix.raw) <= 40:
                return True
    return False


def pattern_probability(pattern: Pattern) -> float:
    """
    指定したEIP-55表示パターンがその位置に出る概算確率。

    raw一致: 1 / 16^len
    英字の大文字小文字一致: 1 / 2^(英字数)

    例:
      ace: raw ace かつ表示 ace -> 1/16^3 * 1/2^3
      cAFE: raw cafe かつ表示 cAFE -> 1/16^4 * 1/2^4
    """
    letter_count = sum(1 for ch in pattern.display if ch.lower() in "abcdef")
    return (1 / (16 ** len(pattern.raw))) * (1 / (2 ** letter_count))


def estimate_match_probability(prefix_patterns, suffix_patterns) -> float:
    """
    prefix候補とsuffix候補の合成一致確率を概算する。

    注意:
      候補同士に包含関係がある場合、厳密値から少しズレる可能性がある。
      通常の ace/Ace/ACE + cafe/Cafe/CAFE のような用途では十分。
    """
    probability = 0.0

    for prefix in prefix_patterns:
        p_prefix = pattern_probability(prefix)

        for suffix in suffix_patterns:
            if len(prefix.raw) + len(suffix.raw) > 40:
                continue

            p_suffix = pattern_probability(suffix)
            probability += p_prefix * p_suffix

    return probability


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def make_mnemonic(words: int) -> str:
    """
    BIP39 mnemonicを生成する。
    12語 = 128-bit entropy
    24語 = 256-bit entropy
    """
    if words == 12:
        entropy = secrets.token_bytes(16)
    elif words == 24:
        entropy = secrets.token_bytes(32)
    else:
        raise ValueError("words must be 12 or 24")

    return str(Bip39MnemonicGenerator().FromEntropy(entropy))


def derive_eth_addresses_for_range(
    mnemonic: str,
    index_start: int,
    index_end: int,
    passphrase: str = "",
):
    """
    1つのmnemonicから、指定されたindex範囲のEthereumアドレスをまとめて導出する。
    """
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate(passphrase)

    base_ctx = (
        Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
    )

    for address_index in range(index_start, index_end + 1):
        ctx = base_ctx.AddressIndex(address_index)
        address = ctx.PublicKey().ToAddress()
        yield address_index, address


def starts_with_any_pattern(body: str, body_lower: str, patterns) -> bool:
    for pattern in patterns:
        if body_lower.startswith(pattern.raw) and body.startswith(pattern.display):
            return True
    return False


def ends_with_any_pattern(body: str, body_lower: str, patterns) -> bool:
    for pattern in patterns:
        if body_lower.endswith(pattern.raw) and body.endswith(pattern.display):
            return True
    return False


def is_match(address: str, prefix_patterns, suffix_patterns) -> bool:
    """
    入力されたprefix/suffix候補に一致するか判定する。

    入力した大文字小文字はEIP-55表示上の厳密一致として扱う。
    例:
      prefix候補: ace,Ace,ACE
      suffix候補: cafe,Cafe,CAFE
    """
    body = address[2:] if address.startswith("0x") else address
    body_lower = body.lower()

    if not starts_with_any_pattern(body, body_lower, prefix_patterns):
        return False

    if not ends_with_any_pattern(body, body_lower, suffix_patterns):
        return False

    return True


def worker(worker_id: int, cfg: dict, stop_event: mp.Event, result_q: mp.Queue):
    words = cfg["words"]
    passphrase = cfg["passphrase"]
    index_start = cfg["index_start"]
    index_end = cfg["index_end"]
    report_every = cfg["report_every"]
    prefix_patterns = cfg["prefix_patterns"]
    suffix_patterns = cfg["suffix_patterns"]

    local_mnemonic_count = 0
    local_address_count = 0

    while not stop_event.is_set():
        mnemonic = make_mnemonic(words)
        local_mnemonic_count += 1

        for address_index, address in derive_eth_addresses_for_range(
            mnemonic=mnemonic,
            index_start=index_start,
            index_end=index_end,
            passphrase=passphrase,
        ):
            local_address_count += 1

            if is_match(address, prefix_patterns, suffix_patterns):
                stop_event.set()
                result_q.put(
                    {
                        "type": "found",
                        "worker_id": worker_id,
                        "mnemonic_count": local_mnemonic_count,
                        "address_count": local_address_count,
                        "mnemonic": mnemonic,
                        "address": address,
                        "address_index": address_index,
                    }
                )
                return

        if local_mnemonic_count % report_every == 0:
            result_q.put(
                {
                    "type": "progress",
                    "mnemonic_count": report_every,
                    "address_count": report_every * (index_end - index_start + 1),
                }
            )


def print_summary(prefix_patterns, suffix_patterns, words, index_start, index_end, workers, passphrase):
    index_count = index_end - index_start + 1
    probability = estimate_match_probability(prefix_patterns, suffix_patterns)
    expected_addresses = (1 / probability) if probability > 0 else None
    expected_mnemonics = (expected_addresses / index_count) if expected_addresses else None

    print()
    print("========================================")
    print("Ethereum mnemonic vanity search")
    print("========================================")
    print("prefix candidates:")
    for p in prefix_patterns:
        print(f"  {p.display}")
    print("suffix candidates:")
    for p in suffix_patterns:
        print(f"  {p.display}")
    print(f"words:            {words}")
    print(f"base path:        {ETH_BASE_PATH}/<index>")
    print(f"index range:      {index_start} - {index_end}")
    print(f"addresses/phrase: {index_count}")
    print(f"workers:          {workers}")
    print(f"passphrase:       {'YES' if passphrase else 'NO'}")

    if expected_addresses:
        print(f"expected checks:  about {expected_addresses:,.0f} addresses")
        print(f"expected tries:   about {expected_mnemonics:,.0f} mnemonics")

    print("========================================")
    print()

    return expected_addresses


def main():
    print("Ethereum Vanity Mnemonic Generator")
    print("入力した大文字小文字は、EIP-55表示上の厳密一致として扱います。")
    print("例: ace,Ace,ACE と入力すると、その3種類だけを許可します。")
    print("使える文字は 0-9, a-f, A-F だけです。")

    prefix_patterns = ask_patterns("先頭")
    suffix_patterns = ask_patterns("末尾")

    if not patterns_are_possible(prefix_patterns, suffix_patterns):
        raise SystemExit("ERROR: 先頭候補 + 末尾候補が40文字を超えるため、成立する組み合わせがありません。")

    words = ask_words()
    index_start, index_end = ask_index_range()
    workers = ask_workers()
    passphrase = ask_passphrase()

    if index_start == 0:
        print()
        print("WARNING: index range includes 0, which is the first Ethereum account.")

    expected_addresses = print_summary(
        prefix_patterns=prefix_patterns,
        suffix_patterns=suffix_patterns,
        words=words,
        index_start=index_start,
        index_end=index_end,
        workers=workers,
        passphrase=passphrase,
    )

    confirm = input("この条件で探索を開始しますか？ [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Cancelled.")
        return

    cfg = {
        "words": words,
        "passphrase": passphrase,
        "index_start": index_start,
        "index_end": index_end,
        "report_every": 5,
        "prefix_patterns": prefix_patterns,
        "suffix_patterns": suffix_patterns,
    }

    stop_event = mp.Event()
    result_q = mp.Queue()

    processes = []
    started = time.time()

    total_mnemonics = 0
    total_addresses = 0

    for worker_id in range(workers):
        p = mp.Process(
            target=worker,
            args=(worker_id, cfg, stop_event, result_q),
            daemon=True,
        )
        p.start()
        processes.append(p)

    try:
        while True:
            try:
                msg = result_q.get(timeout=0.5)

            except queue.Empty:
                elapsed = time.time() - started
                mnemonic_speed = total_mnemonics / elapsed if elapsed > 0 else 0
                address_speed = total_addresses / elapsed if elapsed > 0 else 0

                eta_text = ""
                if expected_addresses and address_speed > 0:
                    eta_seconds = expected_addresses / address_speed
                    eta_text = f" expected_time={format_duration(eta_seconds)}"

                print(
                    f"\rmnemonics={total_mnemonics:,} "
                    f"addresses={total_addresses:,} "
                    f"mnemonic/sec={mnemonic_speed:,.2f} "
                    f"addr/sec={address_speed:,.2f} "
                    f"elapsed={format_duration(elapsed)}"
                    f"{eta_text}",
                    end="",
                    flush=True,
                )
                continue

            if msg["type"] == "progress":
                total_mnemonics += msg["mnemonic_count"]
                total_addresses += msg["address_count"]
                continue

            if msg["type"] == "found":
                stop_event.set()

                total_mnemonics += msg["mnemonic_count"]
                total_addresses += msg["address_count"]

                elapsed = time.time() - started
                mnemonic_speed = total_mnemonics / elapsed if elapsed > 0 else 0
                address_speed = total_addresses / elapsed if elapsed > 0 else 0

                found_index = msg["address_index"]
                found_path = f"{ETH_BASE_PATH}/{found_index}"

                print()
                print()
                print("FOUND")
                print(f"elapsed:          {format_duration(elapsed)}")
                print(f"mnemonics tried:  {total_mnemonics:,}")
                print(f"addresses tried:  {total_addresses:,}")
                print(f"mnemonic/sec:     {mnemonic_speed:,.2f}")
                print(f"address/sec:      {address_speed:,.2f}")
                print()
                print(f"address:          {msg['address']}")
                print(f"address index:    {found_index}")
                print(f"path:             {found_path}")
                print()
                print("mnemonic:")
                print(msg["mnemonic"])
                print()
                print("IMPORTANT:")
                print("Write down ALL of these on paper:")
                print("  1. mnemonic")
                print("  2. path")
                print("  3. address")
                if passphrase:
                    print("  4. passphrase")
                print()
                print("Do NOT save screenshots, cloud notes, chat logs, clipboard copies, or chat messages.")
                break

    except KeyboardInterrupt:
        print()
        print("Interrupted by user.")

    finally:
        stop_event.set()

        for p in processes:
            p.terminate()

        for p in processes:
            p.join()


if __name__ == "__main__":
    mp.freeze_support()
    main()
