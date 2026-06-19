#!/usr/bin/env python3
"""
scripts/solc_compat_wrapper.py
================================
Slither statik analiz aracının çalışması için gereken 'solc' komut satırı
arayüzünü, npm üzerinden kurulu solcjs paketinin (resmî solc'un JS/WASM
derlemesi) --standard-json modunu kullanarak sağlayan bir uyumluluk
katmanıdır. Yalnızca native bir 'solc' ikili dosyasının kurulu OLMADIĞI
ortamlarda (örn. binaries.soliditylang.org'a ağ erişiminin kısıtlı olduğu
korumalı/sandbox ortamlar) gereklidir.

Native solc kuruluyken bu betiğe hiç gerek yoktur; Slither'ı doğrudan
çalıştırabilirsiniz:

    slither contracts/OpticalFormRegistry.sol

Bu betikle çalıştırmak için:

    npm install solc@0.8.24        # solcjs'i PATH'e ekler (node_modules/.bin)
    slither contracts/OpticalFormRegistry.sol --solc scripts/solc_compat_wrapper.py

Ayrıntılar ve gerçek tarama sonuçları için contracts/SLITHER_REPORT.md'ye
bakınız.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOLCJS = shutil.which("solcjs") or "/home/claude/npm_solc/node_modules/.bin/solcjs"


def main() -> int:
    args = sys.argv[1:]

    if "--version" in args:
        print("solc-wrapper, the solidity compiler commandline interface")
        print("Version: 0.8.24+commit.e11b9ed9.wrapper.solcjs")
        return 0

    # Pozisyonel argüman: .sol dosya yolu (ilk '-' ile başlamayan ve .sol ile biten argüman).
    sol_file = None
    for a in args:
        if a.endswith(".sol") and not a.startswith("-"):
            sol_file = a
            break
    if sol_file is None:
        print("error: no .sol input file found in arguments", file=sys.stderr)
        return 1

    source_path = Path(sol_file)
    source_text = source_path.read_text(encoding="utf-8")
    source_key = sol_file  # crytic-compile filename'i argv'deki ile birebir eşleştirir.

    standard_input = {
        "language": "Solidity",
        "sources": {source_key: {"content": source_text}},
        "settings": {
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.bytecode.sourceMap",
                        "evm.deployedBytecode.object",
                        "evm.deployedBytecode.sourceMap",
                        "devdoc",
                        "userdoc",
                    ],
                    "": ["ast"],
                }
            }
        },
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = Path(tmp_dir) / "input.json"
        output_path = Path(tmp_dir) / "output.json"
        input_path.write_text(json.dumps(standard_input), encoding="utf-8")

        with open(input_path, "rb") as stdin_f, open(output_path, "wb") as stdout_f:
            proc = subprocess.run(
                [SOLCJS, "--standard-json"],
                stdin=stdin_f,
                stdout=stdout_f,
                stderr=subprocess.PIPE,
            )
        stdout = output_path.read_text(encoding="utf-8", errors="ignore")

    if "{" not in stdout:
        print(proc.stderr.decode(errors="ignore") if proc.stderr else stdout, file=sys.stderr)
        return 1

    parsed = json.loads(stdout[stdout.index("{"):])

    errors = parsed.get("errors", [])
    fatal = [e for e in errors if e.get("severity") == "error"]
    if fatal:
        for e in fatal:
            print(e.get("formattedMessage", e.get("message", "")), file=sys.stderr)
        return 1

    combined = {"contracts": {}, "sources": {}, "version": "0.8.24"}

    for filename, file_info in parsed.get("sources", {}).items():
        combined["sources"][filename] = {"AST": file_info.get("ast", {})}

    for filename, contracts in parsed.get("contracts", {}).items():
        for contract_name, c in contracts.items():
            key = f"{filename}:{contract_name}"
            evm = c.get("evm", {})
            bytecode = evm.get("bytecode", {})
            deployed = evm.get("deployedBytecode", {})
            combined["contracts"][key] = {
                "abi": c.get("abi", []),
                "bin": bytecode.get("object", ""),
                "bin-runtime": deployed.get("object", ""),
                "srcmap": bytecode.get("sourceMap", ""),
                "srcmap-runtime": deployed.get("sourceMap", ""),
                "userdoc": c.get("userdoc", {}),
                "devdoc": c.get("devdoc", {}),
            }

    print(json.dumps(combined))
    return 0


if __name__ == "__main__":
    sys.exit(main())
