"""准备多运行时桥接所需的嵌入式 Python（cp38 ~ cp312）。

用途：P0「多运行时 IPC 桥接」需要一组与目标券商 xtquant ABI 匹配的极简 Python，
随包放在 backend/runtimes/cpXXX/python.exe，供 ABI 不匹配时拉起桥接子进程。

实现：下载 python.org 的 embed 压缩包（如 python-3.11.9-embed-amd64.zip），
解压到 backend/runtimes/cp311，并写入 .pth 让子进程能 import 本后端 xtquant_client 包。
缺失项跳过（不阻断已有安装），网络不可用时仅告警。

用法：python tools/fetch_runtimes.py            # 准备 cp38~cp312（已存在则跳过）
      python tools/fetch_runtimes.py --only cp311 cp38
      python tools/fetch_runtimes.py --list       # 仅列出将下载的目标
"""
import argparse
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIMES = os.path.join(ROOT, "runtimes")

# 小版本 -> python.org embed 包文件名（amd64）
EMBED = {
    308: "python-3.8.10-embed-amd64.zip",
    309: "python-3.9.13-embed-amd64.zip",
    310: "python-3.10.11-embed-amd64.zip",
    311: "python-3.11.9-embed-amd64.zip",
    312: "python-3.12.7-embed-amd64.zip",
}
BASE_URL = "https://www.python.org/ftp/python/"


def _version_dir(minor: int) -> str:
    major = 3
    patch = {"308": "3.8.10", "309": "3.9.13", "310": "3.10.11",
             "311": "3.11.9", "312": "3.12.7"}[str(minor)]
    return f"{major}.{minor % 100}.{patch.split('.')[-1]}"


def prepare(minor: int, dry: bool = False) -> str:
    """下载并解压指定小版本的嵌入 Python；返回状态描述。"""
    if minor not in EMBED:
        return f"cp{minor}: 未配置下载源"
    dest = os.path.join(RUNTIMES, f"cp{minor}")
    exe = os.path.join(dest, "python.exe")
    if os.path.isfile(exe):
        return f"cp{minor}: 已存在 -> {exe}"
    if dry:
        return f"cp{minor}: 将下载 {EMBED[minor]}"
    ver = _version_dir(minor)
    url = f"{BASE_URL}{ver}/{EMBED[minor]}"
    os.makedirs(dest, exist_ok=True)
    zip_path = os.path.join(dest, EMBED[minor])
    print(f">> 下载 {url}")
    try:
        urllib.request.urlretrieve(url, zip_path)
    except Exception as exc:  # noqa: BLE001
        return f"cp{minor}: 下载失败（{exc}）— 可手动放置 embed 包到 {dest}"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    os.remove(zip_path)
    # 写入 .pth，使子进程能 import 后端 xtquant_client 包（纯 Python）
    pth = os.path.join(dest, "zz_qmt_work_backend.pth")
    with open(pth, "w", encoding="utf-8") as f:
        f.write(ROOT + "\n")
    return f"cp{minor}: 已准备 -> {exe}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="仅准备指定 cpXXX（如 --only cp311 cp38）")
    ap.add_argument("--list", action="store_true", help="仅列出目标，不下载")
    args = ap.parse_args()

    targets = [int(k[2:]) for k in (args.only or [])] if args.only else list(EMBED)
    if args.list:
        for m in targets:
            print(prepare(m, dry=True))
        return
    print(f"目标运行时目录: {RUNTIMES}")
    for m in targets:
        print(prepare(m))
    print("完成。缺失项请检查网络或手动放置 python.org embed 包。")


if __name__ == "__main__":
    main()
