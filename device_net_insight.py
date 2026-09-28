#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_net_insight.py — 本地设备「网络 / WiFi / 蓝牙」体检工具
================================================================

一句话：把你电脑连着什么网、信号好不好、蓝牙连了谁，全部在「本机」查清楚，
        然后用大白话告诉你结论，并画一张好看的仪表盘图。

【隐私承诺】本工具完全在本地运行：
  - 只读取你这台设备自己的系统状态（网卡、WiFi、蓝牙信息）；
  - 不联网、不上传、不把任何数据发给任何服务器；
  - 所有分析、渲染都在你电脑上完成，生成的图片和报告也只存在你指定的本地路径。

子命令：
  collect   采集本机网络 / WiFi / 蓝牙原始数据 -> scan.json
  render    读取 scan.json，做分析 + 画仪表盘 + 写人话报告
  all       先 collect 再 render（一条命令跑完）

用法示例：
  python3 device_net_insight.py all -o ./结果
  python3 device_net_insight.py collect -o ./结果
  python3 device_net_insight.py render -i ./结果/scan.json -o ./结果
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime

# =====================================================================
# 通用工具
# =====================================================================

def run(cmd, timeout=8):
    """安全地执行系统命令，返回合并后的文本；任何异常都返回空串（不崩溃）。"""
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=isinstance(cmd, str),
        )
        return (res.stdout or "") + (res.stderr or "")
    except Exception:
        return ""


def have(cmd):
    return shutil.which(cmd) is not None


def first_nonempty(*vals):
    for v in vals:
        if v:
            return v
    return None


# =====================================================================
# 指标 → 大白话 的映射（给小白看的）
# =====================================================================

def rssi_to_label(dbm):
    """WiFi 信号强度（dBm，越接近 0 越好）转成人话。"""
    if dbm is None:
        return ("未知", 0)
    if dbm >= -50:
        return ("极强（满格，随便跑）", 100)
    if dbm >= -60:
        return ("很好", 88)
    if dbm >= -67:
        return ("不错", 72)
    if dbm >= -70:
        return ("一般（看视频可能偶尔卡）", 55)
    if dbm >= -80:
        return ("偏弱（建议靠近路由器）", 35)
    return ("很弱（容易断线）", 15)


def latency_to_label(ms):
    if ms is None:
        return "无法连通（可能没连上路由器）"
    if ms < 3:
        return "极快（局域网非常顺）"
    if ms < 10:
        return "良好"
    if ms < 30:
        return "一般（有点延迟）"
    return "偏慢（路由器或网络有点堵）"


BAND_PLAIN = {
    "2.4GHz": "2.4G：穿墙远、覆盖大，但速度慢、容易和邻居互相干扰",
    "5GHz": "5G：速度快、干扰少，但穿墙差、离路由器远就掉",
    "6GHz": "6G（Wi-Fi 6E/7）：最新最快、几乎没干扰，但覆盖范围最小",
}

SECURITY_PLAIN = {
    "open": ("危险", "开放网络，谁都能连、能偷看你传的数据，别在上面的网输密码"),
    "wep": ("危险", "WEP 加密早已被破解，等于没锁门"),
    "wpa": ("偏弱", "WPA 较老，建议升级到 WPA2/WPA3"),
    "wpa2": ("安全", "WPA2 是目前主流的安全加密"),
    "wpa3": ("很安全", "WPA3 是最新的强加密，最好"),
}


# =====================================================================
# 采集：按平台分派
# =====================================================================

def _env_kind():
    """判断脚本运行在「本机(bare)」还是「隔离容器(container)」。

    这个 skill 的检测对象 = 脚本运行所在的机器本身。
    若在容器/沙箱里跑，测到的就是容器而非用户的个人设备，必须明确提示。
    """
    if os.path.exists("/.dockerenv"):
        return "container"
    try:
        with open("/proc/1/cgroup", "r") as fh:
            cg = fh.read()
        if any(k in cg for k in ("docker", "kubepods", "containerd", "lxc")):
            return "container"
    except Exception:
        pass
    import re as _re
    host = platform.node() or ""
    if _re.fullmatch(r"[0-9a-fA-F]{12}", host):  # 形如沙箱/容器 id
        return "container"
    return "bare"


def collect():
    sys_name = platform.system()
    data = {
        "meta": {
            "tool": "device-net-insight",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": sys_name,
            "hostname": platform.node(),
            "privacy": "完全本地运行，未联网、未上传任何数据",
            "env_kind": _env_kind(),
            "source": "auto",
        },
        "network": {"interfaces": []},
        "wifi": {},
        "connectivity": {},
        "bluetooth": {},
    }
    if sys_name == "Darwin":
        _collect_macos(data)
    elif sys_name == "Windows":
        _collect_windows(data)
    else:
        _collect_linux(data)
    return data


# ----------------------------- Linux -----------------------------
def _collect_linux(data):
    # 网卡列表（优先用 json 输出，否则回退解析）
    out = run(["ip", "-json", "addr"]) if have("ip") else ""
    if out.strip().startswith("["):
        try:
            addrs = json.loads(out)
            for ifc in addrs:
                name = ifc.get("ifname", "")
                if name == "lo":
                    continue
                ipv4 = ipv6 = None
                for a in ifc.get("addr_info", []):
                    if a.get("family") == "inet":
                        ipv4 = a.get("local")
                    elif a.get("family") == "inet6":
                        ipv6 = a.get("local")
                flags = " ".join(ifc.get("flags", []))
                data["network"]["interfaces"].append({
                    "name": name,
                    "type": _guess_type(name),
                    "up": "UP" in flags,
                    "ipv4": ipv4,
                    "ipv6": ipv6,
                    "mac": ifc.get("address"),
                })
        except Exception:
            pass

    # 默认网关
    gw = None
    route = run(["ip", "route", "show", "default"])
    m = re.search(r"default via (\S+)", route)
    if m:
        gw = m.group(1)
    data["connectivity"]["gateway"] = gw

    # DNS
    dns = []
    resolv = run(["cat", "/etc/resolv.conf"]) if have("cat") else ""
    for line in resolv.splitlines():
        mm = re.search(r"nameserver\s+(\S+)", line)
        if mm:
            dns.append(mm.group(1))
    if not dns:
        dns_out = run(["resolvectl", "status"]) if have("resolvectl") else ""
        for mm in re.finditer(r"DNS Servers:\s*(\S+)", dns_out):
            dns.append(mm.group(1))
    data["connectivity"]["dns"] = dns

    # WiFi（优先 nmcli，回退 iw/iwgetid）
    wifi = {}
    if have("nmcli"):
        dev = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                   "device", "status"])
        for line in dev.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[1] == "wifi" and parts[2] == "connected":
                ifname = parts[0]
                info = run(["nmcli", "-t", "-f",
                            "ACTIVE,SSID,BSSID,SIGNAL,CHAN,FREQ,RATE,SECURITY",
                            "dev", "wifi", "list", "ifname", ifname,
                            "--rescan", "no"])
                for l in info.splitlines():
                    p = l.split(":")
                    if p and p[0] == "yes":
                        wifi = {
                            "connected": True,
                            "ssid": p[1] or None,
                            "bssid": p[2] or None,
                            "signal_pct": _to_int(p[3]),
                            "channel": _to_int(p[4]),
                            "freq_mhz": _to_int(p[5]),
                            "tx_rate_mbps": _parse_rate(p[5] if False else (p[6] if len(p) > 6 else None)),
                            "security": (p[7] if len(p) > 7 else None),
                        }
                    break
                break
    else:
        # 回退：iw / iwgetid
        ssid = run(["iwgetid", "-r"]).strip()
        if ssid:
            link = run(["iwgetid", "-a", "-r"]).strip()  # mac
            wifi = {"connected": True, "ssid": ssid, "bssid": link or None}
    data["wifi"] = wifi or {"connected": False}

    # 蓝牙
    data["bluetooth"] = _bt_linux()

    # 连通性实测（只 ping 本地网关 / DNS，不联网）
    if gw:
        data["connectivity"]["gateway_latency_ms"] = _ping(gw)
    for d in (dns or [])[:1]:
        data["connectivity"]["dns_latency_ms"] = _ping(d)
        break


def _bt_linux():
    bt = {"available": False, "powered": None, "devices": []}
    if have("bluetoothctl"):
        bt["available"] = True
        # 控制器是否通电
        list_out = run(["bluetoothctl", "list"])
        bt["powered"] = ("Controller" in list_out)
        # 已配对设备
        devs = run(["bluetoothctl", "devices"])
        connected = run(["bluetoothctl", "devices", "Connected"])
        connected_macs = set()
        for l in connected.splitlines():
            mm = re.search(r"Device (\S+)", l)
            if mm:
                connected_macs.add(mm.group(1).lower())
        for l in devs.splitlines():
            mm = re.search(r"Device (\S+)\s+(.*)", l)
            if mm:
                mac = mm.group(1)
                bt["devices"].append({
                    "name": mm.group(2).strip() or mac,
                    "mac": mac,
                    "connected": mac.lower() in connected_macs,
                })
    elif have("hciconfig"):
        bt["available"] = True
        bt["powered"] = ("UP" in run(["hciconfig"]))
    return bt


# ----------------------------- macOS -----------------------------
def _collect_macos(data):
    # 网卡
    ifc_out = run(["ifconfig", "-a"])
    for block in ifc_out.split("\n\n"):
        m = re.match(r"(\w+):", block)
        if not m or m.group(1) == "lo0":
            continue
        name = m.group(1)
        up = "status: active" in block or "flags=.*<.*UP.*>" in block
        ipv4 = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", block)
        ipv6 = re.search(r"inet6 (\S+)", block)
        mac = re.search(r"ether (\S+)", block)
        data["network"]["interfaces"].append({
            "name": name,
            "type": _guess_type(name),
            "up": bool(up),
            "ipv4": ipv4.group(1) if ipv4 else None,
            "ipv6": ipv6.group(1) if ipv6 else None,
            "mac": mac.group(1) if mac else None,
        })

    # 默认网关 + DNS
    route = run(["route", "-n", "get", "default"])
    mg = re.search(r"gateway:\s*(\S+)", route)
    data["connectivity"]["gateway"] = mg.group(1) if mg else None
    dns_out = run(["scutil", "--dns"])
    dns = re.findall(r"nameserver\[\d+\] : (\S+)", dns_out)
    data["connectivity"]["dns"] = dns

    # WiFi（airport 细节 + networksetup 兜底）
    airport = "/System/Library/PrivateFrameworks/Apple80211.framework/" \
              "Versions/Current/Resources/airport"
    wifi = {}
    if os.path.exists(airport):
        info = run([airport, "-I"])
        ssid = re.search(r"\bSSID:\s*(.+)", info)
        rssi = re.search(r"agrCtlRSSI:\s*(-?\d+)", info)
        chan = re.search(r"channel:\s*(\d+)", info)
        if ssid and ssid.group(1).strip():
            wifi = {
                "connected": True,
                "ssid": ssid.group(1).strip(),
                "rssi_dbm": _to_int(rssi.group(1)) if rssi else None,
                "channel": _to_int(chan.group(1)) if chan else None,
                "bssid": (re.search(r"BSSID:\s*(\S+)", info).group(1)
                          if re.search(r"BSSID:\s*(\S+)", info) else None),
                "security": (re.search(r"link auth:\s*(\S+)", info).group(1)
                             if re.search(r"link auth:\s*(\S+)", info) else None),
            }
    else:
        ssid = run(["networksetup", "-getairportnetwork", "en0"]).strip()
        if ":" in ssid:
            wifi = {"connected": True, "ssid": ssid.split(":", 1)[1].strip()}
    data["wifi"] = wifi or {"connected": False}

    # 蓝牙
    bt_out = run(["system_profiler", "SPBluetoothDataType"])
    bt = {"available": "Bluetooth" in bt_out, "powered": "Powered: Yes" in bt_out,
          "devices": []}
    for l in bt_out.splitlines():
        mm = re.search(r"^\s{4}(.+?):$", l)
        if mm and ("Connected: Yes" in bt_out or "Connected: Yes" in l):
            pass
    data["bluetooth"] = bt

    # 连通性实测（只 ping 本地）
    if data["connectivity"]["gateway"]:
        data["connectivity"]["gateway_latency_ms"] = _ping(
            data["connectivity"]["gateway"], "-t")
    for d in (dns or [])[:1]:
        data["connectivity"]["dns_latency_ms"] = _ping(d, "-t")
        break


# ----------------------------- Windows -----------------------------
def _collect_windows(data):
    # 网卡 + IP + 网关 + DNS（解析 ipconfig /all）
    cfg = run(["ipconfig", "/all"])
    cur = None
    for line in cfg.splitlines():
        m = re.match(r"\r?\n?(\S[^:]*?)\s+adapter\s+(.+):", line)
        if m:
            cur = {"name": m.group(2).strip(), "type": "other", "up": False,
                   "ipv4": None, "ipv6": None, "mac": None,
                   "dns": [], "gateway": None}
            data["network"]["interfaces"].append(cur)
            continue
        if cur is None:
            continue
        if "Physical Address" in line:
            mm = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", line)
            if mm:
                cur["mac"] = mm.group(0)
        if "IPv4 Address" in line:
            mm = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if mm:
                cur["ipv4"] = mm.group(1)
        if "IPv6 Address" in line:
            mm = re.search(r"([0-9a-fA-F:]+)", line)
            if mm:
                cur["ipv6"] = mm.group(1)
        if "Default Gateway" in line:
            mm = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if mm:
                cur["gateway"] = mm.group(1)
        if "DNS Servers" in line:
            for mm in re.finditer(r"(\d+\.\d+\.\d+\.\d+)", line):
                cur["dns"].append(mm.group(1))
        if "Media State" not in line and ("IPv4 Address" in line):
            cur["up"] = True

    gw = None
    for ifc in data["network"]["interfaces"]:
        if ifc.get("gateway"):
            gw = ifc["gateway"]
            break
    data["connectivity"]["gateway"] = gw
    all_dns = []
    for ifc in data["network"]["interfaces"]:
        all_dns.extend(ifc.get("dns", []))
    data["connectivity"]["dns"] = list(dict.fromkeys(all_dns))

    # WiFi（netsh）
    wifi_out = run(["netsh", "wlan", "show", "interfaces"])
    wifi = {"connected": False}
    if "SSID" in wifi_out:
        ssid = re.search(r"SSID\s*:\s*(.+)", wifi_out)
        bssid = re.search(r"BSSID\s*:\s*(\S+)", wifi_out)
        signal = re.search(r"Signal\s*:\s*(\d+)%", wifi_out)
        chan = re.search(r"Channel\s*:\s*(\d+)", wifi_out)
        radio = re.search(r"Radio type\s*:\s*(.+)", wifi_out)
        auth = re.search(r"Authentication\s*:\s*(.+)", wifi_out)
        wifi = {
            "connected": True,
            "ssid": ssid.group(1).strip() if ssid else None,
            "bssid": bssid.group(1) if bssid else None,
            "signal_pct": _to_int(signal.group(1)) if signal else None,
            "channel": _to_int(chan.group(1)) if chan else None,
            "radio_type": radio.group(1).strip() if radio else None,
            "security": auth.group(1).strip() if auth else None,
        }
    data["wifi"] = wifi

    # 蓝牙（PowerShell）
    bt = {"available": False, "powered": None, "devices": []}
    ps = run(["powershell", "-command",
              "Get-PnpDevice -Class Bluetooth | Select-Object Status,FriendlyName "
              "| Format-Table -HideTableHeaders"])
    if ps.strip():
        bt["available"] = True
        for l in ps.splitlines():
            l = l.strip()
            if l:
                bt["devices"].append({"name": l, "connected": "OK" in l})
        bt["powered"] = any("OK" in l for l in ps.splitlines())
    data["bluetooth"] = bt

    # 连通性实测（只 ping 本地）
    if gw:
        data["connectivity"]["gateway_latency_ms"] = _ping_win(gw)
    for d in (data["connectivity"]["dns"] or [])[:1]:
        data["connectivity"]["dns_latency_ms"] = _ping_win(d)
        break


# ----------------------------- 连通性辅助 -----------------------------
def _ping(host, flag="-c"):
    out = run(["ping", flag, "1", "-W", "2", host], timeout=6)
    m = re.search(r"time[=<]?\s*([\d.]+)\s*ms", out)
    if m:
        return float(m.group(1))
    if "ttl=" in out.lower() or "bytes from" in out.lower():
        return 1.0
    return None


def _ping_win(host):
    out = run(["ping", "-n", "1", "-w", "2000", host], timeout=6)
    m = re.search(r"时间[=<]?\s*([\d.]+)\s*ms", out) or \
        re.search(r"time[=<]?\s*([\d.]+)\s*ms", out)
    if m:
        return float(m.group(1))
    return None


# ----------------------------- 小工具 -----------------------------
def _to_int(s):
    try:
        return int(str(s).strip())
    except Exception:
        return None


def _parse_rate(s):
    try:
        return float(str(s).replace(" MBit/s", "").strip())
    except Exception:
        return None


def _guess_type(name):
    n = name.lower()
    if "wi" in n or "wlan" in n or "en0" == n:
        return "wifi"
    if "eth" in n or "en" in n and "en0" != n:
        return "ethernet"
    if "lo" in n:
        return "loopback"
    return "other"


def _band_from_freq(freq_mhz):
    if not freq_mhz:
        return None
    if freq_mhz < 3000:
        return "2.4GHz"
    if freq_mhz < 6000:
        return "5GHz"
    return "6GHz"


def _band_from_channel(ch):
    if not ch:
        return None
    if ch <= 14:
        return "2.4GHz"
    if ch < 33 or (36 <= ch <= 64) or (100 <= ch <= 144):
        return "5GHz"
    return "5GHz"


# ----------------------- 用户提供数据（读不到真机时的备用路径） -----------------------

def _parse_user_text(text):
    """尽力从用户粘贴的 ipconfig / netsh 输出或自由描述里提取关键信息。
    提取不到的字段留空，由 analyze 优雅降级，绝不编造。"""
    text = text or ""
    net = {"interfaces": []}
    wifi = {}
    conn = {}
    bt = {"available": None, "powered": None, "devices": []}

    # 网卡 / IP / 网关 / DNS：按 ipconfig 风格逐行解析（兼容中英文系统）
    cur = None
    in_dns = False
    for line in text.splitlines():
        s = line.strip()
        if re.match(r".*adapter\s+(.+):", s) and \
           "ipv4" not in s.lower() and "gateway" not in s.lower() and "dns" not in s.lower():
            cur = {"name": re.match(r".*adapter\s+(.+):", s).group(1).strip(),
                   "type": "other", "up": False, "ipv4": None, "ipv6": None,
                   "mac": None, "dns": [], "gateway": None}
            net["interfaces"].append(cur)
            in_dns = False
            continue
        low = s.lower()
        if "physical address" in low or "物理地址" in s:
            mm = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", s)
            if mm and cur is not None:
                cur["mac"] = mm.group(0)
        if "ipv4" in low and ("地址" in s or "address" in low):
            mm = re.search(r"(\d+\.\d+\.\d+\.\d+)", s)
            if mm and cur is not None:
                cur["ipv4"] = mm.group(1)
                cur["up"] = True
        if "默认网关" in s or "default gateway" in low:
            mm = re.search(r"(\d+\.\d+\.\d+\.\d+)", s)
            if mm and cur is not None:
                cur["gateway"] = mm.group(1)
        if "dns" in low and ("服务器" in s or "servers" in low):
            in_dns = True
            if cur is not None:
                for mm in re.finditer(r"(\d+\.\d+\.\d+\.\d+)", s):
                    cur["dns"].append(mm.group(1))
            continue
        # DNS 续行：缩进的纯 IP 行（ipconfig 多 DNS 换行显示）
        if in_dns and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", s) and cur is not None:
            cur["dns"].append(s)
            continue
        if s:
            in_dns = False

    for ifc in net["interfaces"]:
        if ifc.get("gateway"):
            conn["gateway"] = ifc["gateway"]
            break
    dns_all = []
    for ifc in net["interfaces"]:
        dns_all.extend(ifc.get("dns", []))
    if dns_all:
        conn["dns"] = list(dict.fromkeys(dns_all))
    if net["interfaces"]:
        net["interfaces"][0].setdefault("type", _guess_type(net["interfaces"][0]["name"]))

    # WiFi（netsh wlan show interfaces，兼容中英文）
    ssid = re.search(r"SSID\s*[\:\：]\s*(.+)", text)
    if ssid and ssid.group(1).strip():
        wifi["connected"] = True
        wifi["ssid"] = ssid.group(1).strip()
        bssid = re.search(r"BSSID\s*[\:\：]\s*(\S+)", text)
        if bssid:
            wifi["bssid"] = bssid.group(1)
        sig = re.search(r"(?:Signal|信号)\s*[\:\：]\s*(\d+)\s*%", text)
        if sig:
            wifi["signal_pct"] = _to_int(sig.group(1))
        ch = re.search(r"(?:Channel|信道)\s*[\:\：]\s*(\d+)", text)
        if ch:
            wifi["channel"] = _to_int(ch.group(1))
        auth = re.search(r"(?:Authentication|身份验证|加密|安全)\s*[\:\：]\s*(.+)", text)
        if auth:
            wifi["security"] = auth.group(1).strip()
    else:
        wifi["connected"] = bool(re.search(r"连著?wifi|连接.*wifi|wifi.*连接|无线.*连", text, re.I))
        ssid2 = re.search(r"wifi\s*(名称|叫|名)?\s*[:：]?\s*([^\s,，。；;]+)", text, re.I)
        if ssid2:
            wifi["ssid"] = ssid2.group(2)
            wifi["connected"] = True

    # 中文自由描述兜底（IP是 X / 网关 X / DNS X / 信号 X%），只在缺失时补
    ip_cn = re.search(r"IP\s*(地址|是)?\s*[:：]?\s*(\d{1,3}(?:\.\d{1,3}){3})", text, re.I)
    if ip_cn and not any(i.get("ipv4") for i in net["interfaces"]):
        net["interfaces"].append({"name": "手动录入", "type": "wifi", "up": True,
                                  "ipv4": ip_cn.group(2), "ipv6": None,
                                  "mac": None, "dns": [], "gateway": None})
    gw_cn = re.search(r"网关\s*[:：]?\s*(\d{1,3}(?:\.\d{1,3}){3})", text)
    if gw_cn and not conn.get("gateway"):
        conn["gateway"] = gw_cn.group(1)
    dns_cn = re.search(r"DNS\s*(服务器|是)?\s*[:：]?\s*(\d{1,3}(?:\.\d{1,3}){3})", text, re.I)
    if dns_cn:
        conn.setdefault("dns", [])
        if dns_cn.group(2) not in conn["dns"]:
            conn["dns"].append(dns_cn.group(2))
    sig_cn = re.search(r"信号\s*(\d+)\s*%?", text)
    if sig_cn and wifi.get("connected"):
        wifi.setdefault("signal_pct", _to_int(sig_cn.group(1)))

    return {"network": net, "wifi": wifi, "connectivity": conn, "bluetooth": bt}


def _interactive_manual():
    """本机手动录入（用户在自己电脑上想手填时用）。"""
    print("—— 手动录入模式（读不到真机时的备用方式）——")
    print("看不懂的可以直接回车跳过；IP 地址尽量填。")
    ip = input("你的 IPv4 地址（如 192.168.1.23）：").strip()
    gw = input("路由器/网关地址（如 192.168.1.1，没有就回车）：").strip()
    dns = input("DNS 服务器（多个用逗号，如 223.5.5.5,8.8.8.8）：").strip()
    ssid = input("WiFi 名称（没连 WiFi 就回车）：").strip()
    sig = input("WiFi 信号百分比（如 75，没有就回车）：").strip()
    bt = input("已连接的蓝牙设备名（多个用逗号，没有就回车）：").strip()
    net = {"interfaces": []}
    if ip:
        net["interfaces"].append({"name": "手动录入", "type": "wifi",
                                  "up": True, "ipv4": ip, "ipv6": None,
                                  "mac": None, "dns": [], "gateway": gw or None})
    conn = {}
    if gw:
        conn["gateway"] = gw
    if dns:
        conn["dns"] = [x.strip() for x in dns.split(",") if x.strip()]
    wifi = {}
    if ssid:
        wifi["connected"] = True
        wifi["ssid"] = ssid
        if sig:
            wifi["signal_pct"] = _to_int(sig)
    btd = {"available": None, "powered": None, "devices": []}
    if bt:
        for n in bt.split(","):
            n = n.strip()
            if n:
                btd["devices"].append({"name": n, "connected": True})
        btd["available"] = True
    return {"network": net, "wifi": wifi, "connectivity": conn, "bluetooth": btd}


def build_user_data(partial, note=None):
    partial = partial or {}
    return {
        "meta": {
            "tool": "device-net-insight",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": "用户提供 / 未知",
            "hostname": note or "你的设备",
            "privacy": "完全本地运行，未联网、未上传任何数据",
            "env_kind": "user-provided",
            "source": "user-provided",
        },
        "network": partial.get("network", {"interfaces": []}),
        "wifi": partial.get("wifi", {}),
        "connectivity": partial.get("connectivity", {}),
        "bluetooth": partial.get("bluetooth", {"devices": []}),
    }


# =====================================================================
# 分析：把原始数据变成「人话结论」
# =====================================================================

def analyze(data):
    findings = []
    wifi = data.get("wifi", {})
    conn = data.get("connectivity", {})
    bt = data.get("bluetooth", {})
    manual = data.get("meta", {}).get("source") == "user-provided"

    # --- WiFi ---
    if wifi.get("connected"):
        # 信号
        rssi = wifi.get("rssi_dbm")
        pct = wifi.get("signal_pct")
        if rssi is not None:
            label, score = rssi_to_label(rssi)
            findings.append(_f("wifi", "good" if score >= 72 else
                                ("warn" if score >= 35 else "bad"),
                                "WiFi 信号强度",
                                f"当前信号：{label}（{rssi} dBm）。",
                                "dBm 是信号强度单位，数字越接近 0 越强；-67 以上算好，-80 以下就容易断。"))
        elif pct is not None:
            findings.append(_f("wifi", "good" if pct >= 70 else
                                ("warn" if pct >= 40 else "bad"),
                                "WiFi 信号强度",
                                f"当前信号：约 {pct}%（{'好' if pct>=70 else '一般' if pct>=40 else '弱'}）。",
                                "信号百分比越高越稳。"))
        # 频段
        band = _band_from_freq(wifi.get("freq_mhz")) or \
               _band_from_channel(wifi.get("channel"))
        if band:
            findings.append(_f("wifi", "good",
                                "WiFi 频段",
                                f"你连在 {band}。{BAND_PLAIN.get(band, '')}",
                                "频段决定速度和覆盖：2.4G 远但慢，5G/6G 快但近。"))
        # 安全
        sec = (wifi.get("security") or "").lower()
        key = "open" if "open" in sec else (
              "wep" if "wep" in sec else (
              "wpa3" if "wpa3" in sec else (
              "wpa2" if "wpa2" in sec else (
              "wpa" if "wpa" in sec else "other"))))
        if key in SECURITY_PLAIN:
            lvl, tip = SECURITY_PLAIN[key]
            findings.append(_f("wifi",
                               "bad" if key in ("open", "wep") else
                               ("warn" if key == "wpa" else "good"),
                               "WiFi 加密安全",
                               f"加密方式：{wifi.get('security')} —— {lvl}。{tip}",
                               "加密决定别人能不能偷看你上网的内容。"))
    else:
        if manual:
            findings.append(_f("wifi", "warn", "WiFi 连接",
                               "你没有提供 WiFi 信息（此项未填写）。",
                               "没填的项不会瞎猜；想更全就在你电脑上直接运行本脚本。"))
        else:
            findings.append(_f("wifi", "warn", "WiFi 连接",
                               "当前没有连上 WiFi（或检测不到）。",
                               "如果是插网线上网，这很正常；否则检查一下 WiFi 开关。"))

    # --- 连通性 ---
    gw = conn.get("gateway")
    if gw:
        lat = conn.get("gateway_latency_ms")
        if lat is not None:
            findings.append(_f("net", "good" if lat < 10 else "warn",
                                "到路由器延迟",
                                f"你到路由器的往返延迟：{lat} ms —— "
                                f"{latency_to_label(lat)}。",
                                "这是你电脑到「家门口路由器」的速度，越小越顺。"))
        elif manual:
            findings.append(_f("net", "warn", "到路由器延迟",
                                "延迟未测（你没有提供，本工具不会替你猜）。",
                                "想自动测延迟，就在你电脑上直接运行本脚本。"))
        else:
            findings.append(_f("net", "bad", "到路由器延迟",
                                "到路由器不通（ping 不到）。",
                                "连路由器都不通，说明网络基本断了，先看网线/WiFi。"))
    else:
        if manual:
            findings.append(_f("net", "warn", "网关",
                               "你没有提供网关地址（此项未填写）。",
                               "网关就是路由器，填上它才能判断连没连上网。"))
        else:
            findings.append(_f("net", "bad", "网关",
                               "没找到默认网关，可能没正常连网。",
                               "网关就是路由器，找不到它就等于没连上网络。"))
    if conn.get("dns"):
        findings.append(_f("net", "good", "DNS",
                           f"DNS 服务器：{', '.join(conn['dns'][:3])}。"
                           "DNS 负责把网址变成 IP，能解析就说明上网通畅。",
                           "DNS 异常会导致「能上微信但打不开网页」。"))
    else:
        findings.append(_f("net", "warn", "DNS",
                           "你没有提供 DNS 信息（此项未填写）。" if manual
                           else "没检测到 DNS 服务器。",
                           "没有 DNS，可能只能上 QQ/微信，打不开网页。"))

    # --- 蓝牙 ---
    if bt.get("available"):
        devs = bt.get("devices", [])
        connected = [d for d in devs if d.get("connected")]
        findings.append(_f("bt", "good", "蓝牙",
                           f"蓝牙已开启，共发现 {len(devs)} 个设备，"
                           f"其中 {len(connected)} 个已连接"
                           f"（{', '.join(d['name'] for d in connected[:5]) or '无'}）。",
                           "蓝牙连接太多可能互相抢带宽、更费电；不认识的设备建议删掉。"))
        if len(connected) >= 4:
            findings.append(_f("bt", "warn", "蓝牙数量",
                               "已连接蓝牙设备偏多（≥4 个）。",
                               "设备多了容易卡顿、掉线、更耗电，用不上的可以断开。"))
    else:
        findings.append(_f("bt", "warn", "蓝牙",
                           "蓝牙信息你未提供（此项未填写）。" if manual
                           else "未检测到蓝牙（可能本机没有蓝牙或已关闭）。",
                           "没有蓝牙也很正常，不影响上网。"))

    # 总体评分
    score = sum(1 for f in findings if f["level"] == "good") * 10 \
            + sum(1 for f in findings if f["level"] == "warn") * 5
    total = len(findings) * 10
    grade = "优秀" if score >= total * 0.85 else \
            "良好" if score >= total * 0.6 else \
            "需注意" if score >= total * 0.4 else "有问题"
    return {"findings": findings, "score": score, "total": total,
            "grade": grade}


def _f(area, level, title, plain, explain):
    return {"area": area, "level": level, "title": title,
            "plain": plain, "explain": explain}


# =====================================================================
# 渲染：Ins 风暗色仪表盘
# =====================================================================

def render(data, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    import matplotlib.font_manager as fm

    os.makedirs(out_dir, exist_ok=True)
    analysis = analyze(data)
    manual = data.get("meta", {}).get("source") == "user-provided"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 字体（中文，找不到就退回默认）
    cn_font = _pick_cn_font()
    if cn_font:
        plt.rcParams["font.family"] = fm.FontProperties(fname=cn_font).get_name()
    plt.rcParams["axes.unicode_minus"] = False

    # Ins 渐变配色
    GRAD = ["#F58529", "#DD2A7B", "#8134AF", "#515BD4"]
    BG = "#0A0A0A"
    TXT = "#F5F5F5"
    MUT = "#9A9A9A"
    LEVEL = {"good": "#4ADE80", "warn": "#FBBF24", "bad": "#F87171"}

    fig = plt.figure(figsize=(11, 8.2), facecolor=BG)
    fig.subplots_adjust(left=0.06, right=0.96, top=0.90, bottom=0.06,
                        wspace=0.25, hspace=0.42)

    # 标题
    fig.text(0.06, 0.955, "设备连接体检报告", fontsize=24, fontweight="bold",
             color=TXT)
    fig.text(0.06, 0.925,
             f"检测对象：{data['meta']['hostname']} · "
             f"{data['meta']['platform']} · "
             f"生成于 {data['meta']['generated_at']}", fontsize=10, color=MUT)
    kind = data["meta"].get("env_kind")
    if kind == "container":
        fig.text(0.06, 0.905,
                 "注意：当前运行在隔离/容器环境 —— 以上数据描述该环境，"
                 "并非你的个人设备",
                 fontsize=9, color="#F58529")
    elif kind == "user-provided":
        fig.text(0.06, 0.905,
                 "以下数据由你提供（非本机自动读取）· 未联网 · 仅供你参考",
                 fontsize=9, color="#F58529")
    else:
        fig.text(0.06, 0.905, "完全本地生成 · 未联网 · 未上传任何数据",
                 fontsize=9, color="#8134AF")

    # 顶部 Instagram 渐变装饰条
    try:
        import numpy as np
        from matplotlib.colors import LinearSegmentedColormap
        axg = fig.add_axes([0.06, 0.892, 0.88, 0.007])
        axg.axis("off")
        cmap = LinearSegmentedColormap.from_list("ins", GRAD)
        axg.imshow(np.linspace(0, 1, 256).reshape(1, -1), aspect="auto",
                   cmap=cmap, extent=[0, 1, 0, 1])
    except Exception:
        pass

    # 左上：总体评分
    ax0 = fig.add_axes([0.06, 0.60, 0.40, 0.25], facecolor=BG)
    ax0.axis("off")
    ax0.text(0, 0.98, "总体评价", fontsize=13, color=MUT, va="top")
    ax0.text(0, 0.45, analysis["grade"], fontsize=40, fontweight="bold",
             color=GRAD[1], va="center")
    ratio = analysis["score"] / analysis["total"] if analysis["total"] else 0
    ax0.text(0, 0.06, f"健康度 {analysis['score']}/{analysis['total']} "
             f"（{ratio*100:.0f}%）", fontsize=12, color=TXT, va="bottom")

    # 右上：连通性拓扑
    ax1 = fig.add_axes([0.52, 0.60, 0.42, 0.25], facecolor=BG)
    ax1.axis("off")
    ax1.set_xlim(-0.4, 3.4); ax1.set_ylim(0, 1)
    conn = data.get("connectivity", {})
    nodes = [("本机", "#515BD4"),
             ("路由器", "#4ADE80" if conn.get("gateway") else "#F87171"),
             ("网络", "#4ADE80" if conn.get("dns") else "#FBBF24")]
    for i, (label, col) in enumerate(nodes):
        x = i
        if i < 2:
            ax1.plot([x + 0.24, x + 0.76], [0.5, 0.5], color=col, lw=3,
                     alpha=0.8, solid_capstyle="round")
        ax1.add_patch(FancyBboxPatch((x - 0.24, 0.28), 0.48, 0.44,
                     boxstyle="round,pad=0.02,rounding_size=0.10",
                     linewidth=0, facecolor=col, alpha=0.85))
        ax1.text(x, 0.5, label, color="white", ha="center", va="center",
                 fontsize=9.5, fontweight="bold")
    ax1.text(1.5, 0.82, "连接链路", fontsize=12, color=MUT, ha="center")

    # 左下：WiFi 信号条（标题用 axes 坐标，避免与上方面板重叠）
    ax2 = fig.add_axes([0.06, 0.36, 0.40, 0.19], facecolor=BG)
    ax2.axis("off")
    ax2.set_xlim(0, 100); ax2.set_ylim(-0.5, 0.5)
    wifi = data.get("wifi", {})
    if wifi.get("connected"):
        rssi = wifi.get("rssi_dbm")
        pct = wifi.get("signal_pct")
        if rssi is not None:
            _, val = rssi_to_label(rssi)
        elif pct is not None:
            val = pct
        else:
            val = 50
        ax2.text(0, 0.98, f"WiFi 信号：{(wifi.get('ssid') or '')[:18]}",
                 fontsize=12, color=TXT, va="top", transform=ax2.transAxes)
        bar_color = GRAD[0] if val >= 72 else (GRAD[2] if val >= 35 else "#F87171")
        ax2.barh(0, 100, color="#222", height=0.6, left=0, zorder=0)
        ax2.barh(0, val, color=bar_color, height=0.6, edgecolor="none", zorder=1)
        ax2.text(val + 2, 0, f"{val}%", color=TXT, va="center", fontsize=12,
                 zorder=2)
    else:
        ax2.text(0, 0.5, "WiFi 信息未提供" if manual else "未连接 WiFi",
                 fontsize=13, color=MUT, va="center",
                 transform=ax2.transAxes)

    # 右下：蓝牙设备（固定坐标，避免 autoscale 抖动导致文字错位）
    ax3 = fig.add_axes([0.52, 0.36, 0.42, 0.19], facecolor=BG)
    ax3.axis("off")
    ax3.set_xlim(0, 1); ax3.set_ylim(0, 1)
    bt = data.get("bluetooth", {})
    devs = bt.get("devices", [])
    bt_head = "蓝牙设备：未提供" if (manual and not devs) else (
        f"蓝牙设备：{len(devs)} 个"
        f"（已连 {sum(1 for d in devs if d.get('connected'))}）")
    ax3.text(0, 0.98, bt_head,
             fontsize=12, color=TXT, va="top", transform=ax3.transAxes)
    if devs:
        shown = devs[:6]
        names = [d["name"][:14] for d in shown]
        cols = [LEVEL["good"] if d.get("connected") else MUT for d in shown]
        step = min(0.16, 0.86 / len(shown))
        y = 0.74
        for n, c in zip(names, cols):
            ax3.add_patch(plt.Circle((0.035, y), 0.028, color=c))
            ax3.text(0.10, y, n, color=TXT, va="center", fontsize=10)
            y -= step
    else:
        ax3.text(0, 0.5, "蓝牙信息未提供" if manual else "无蓝牙设备 / 未开启",
                 color=MUT, va="center", fontsize=11, transform=ax3.transAxes)

    # 底部：体检结论列表（按宽度折行 + 行距自适应，避免溢出/重叠/截断）
    ax4 = fig.add_axes([0.06, 0.05, 0.88, 0.26], facecolor=BG)
    ax4.axis("off")
    ax4.text(0, 1.06, "一句话结论", fontsize=13, color=MUT, va="top")
    import textwrap

    def _wrap_cn(s, width):
        out = []
        for para in str(s).split("\n"):
            out.extend(textwrap.wrap(para, width) or [""])
        return out

    fnds = analysis["findings"]
    groups = [(f["level"], _wrap_cn(f["plain"], 52)) for f in fnds]
    total_lines = max(sum(len(w) for _, w in groups), 1)
    top = 0.86
    line_h = min(0.17, (top - 0.02) / total_lines)
    fs = 9.5 if total_lines <= 6 else (8.6 if total_lines <= 9 else 8.0)
    y = top
    for lvl, ls in groups:
        for li, line in enumerate(ls):
            if li == 0:
                ax4.add_patch(plt.Rectangle((0, y - 0.028), 0.016, 0.056,
                             color=LEVEL[lvl]))
            ax4.text(0.030, y, line, color=TXT, va="center", fontsize=fs)
            y -= line_h
            if y < 0.0:
                break
        if y < 0.0:
            break

    png = os.path.join(out_dir, f"连接体检仪表盘_{ts}.png")
    fig.savefig(png, dpi=150, facecolor=BG)
    plt.close(fig)

    # 人话报告（txt）
    txt = os.path.join(out_dir, f"连接体检报告_{ts}.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(_report_text(data, analysis))

    return png, txt


def _report_text(data, analysis):
    L = []
    L.append("=" * 48)
    L.append("       设备连接体检报告（大白话版）")
    L.append("=" * 48)
    L.append("")
    L.append("【隐私说明】本报告完全在本机生成，")
    L.append("没有联网、没有把任何数据发到网上。")
    L.append("")
    L.append(f"【本次检测对象】{data['meta']['hostname']}"
             f"（{data['meta']['platform']}）")
    if data["meta"].get("env_kind") == "container":
        L.append("")
        L.append("⚠️  注意：检测到当前运行在「隔离/容器环境」中，")
        L.append("    上面这份数据描述的是这个隔离环境，")
        L.append("    并不是你的个人设备。")
        L.append("    想体检你自己的设备，请在那台设备上直接运行本脚本。")
    elif data["meta"].get("source") == "user-provided":
        L.append("")
        L.append("ℹ️  注意：这份报告的数据是你自己提供的，")
        L.append("    不是本工具在你的设备上自动读取的。")
        L.append("    没填的项是真不知道，绝不会瞎编；")
        L.append("    想看最全的结果，请在你的 Windows 上直接运行本脚本。")
    L.append(f"生成时间：{data['meta']['generated_at']}")
    L.append(f"总体评价：{analysis['grade']}（健康度 "
             f"{analysis['score']}/{analysis['total']}）")
    L.append("")
    L.append("-" * 48)
    L.append("逐项说明：")
    L.append("-" * 48)
    icon = {"good": "✅", "warn": "⚠️", "bad": "❌"}
    for f in analysis["findings"]:
        L.append(f"\n{icon[f['level']]} {f['title']}")
        L.append(f"   {f['plain']}")
        L.append(f"   （科普：{f['explain']}）")
    L.append("")
    L.append("-" * 48)
    L.append("给小白的 3 条建议：")
    L.append("-" * 48)
    L.append("1. 上网卡？先靠近路由器，或把 2.4G 换成 5G 频段试试。")
    L.append("2. 公共 WiFi 别输密码、别付款；家里路由器设 WPA2/WPA3。")
    L.append("3. 蓝牙用不上的设备就断开，连太多会卡、会费电。")
    L.append("")
    if data["meta"].get("source") == "user-provided":
        L.append("（本报告基于你提供的数据在本地生成；本工具不上传任何数据。）")
    else:
        L.append("（本工具只读取你设备自身状态，所有结论均为本地分析。）")
    return "\n".join(L)


def _pick_cn_font():
    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


# =====================================================================
# 主入口
# =====================================================================

def main():
    ap = argparse.ArgumentParser(description="本地设备网络/WiFi/蓝牙体检工具")
    sub = ap.add_subparsers(dest="cmd")

    pc = sub.add_parser("collect", help="采集原始数据 -> scan.json")
    pc.add_argument("-o", "--out", default="./结果", help="输出目录")

    pr = sub.add_parser("render", help="分析 + 渲染")
    pr.add_argument("-i", "--in", default=None, help="scan.json 路径")
    pr.add_argument("-o", "--out", default="./结果", help="输出目录")

    pa = sub.add_parser("all", help="采集 + 分析 + 渲染")
    pa.add_argument("-o", "--out", default="./结果", help="输出目录")

    pm = sub.add_parser("manual",
                        help="读不到真机时，用你提供的数据出报告")
    pm.add_argument("--text", default=None,
                    help="粘贴 ipconfig / netsh 输出或你看到的信息")
    pm.add_argument("--json", dest="json_file", default=None,
                    help="直接给一个部分 scan.json（你填好的字段）")
    pm.add_argument("-o", "--out", default="./结果", help="输出目录")

    args = ap.parse_args()
    cmd = args.cmd or "all"

    print("🔒 本工具完全在本地运行，不联网、不上传任何数据。")

    if cmd in ("collect", "all"):
        data = collect()
        os.makedirs(args.out, exist_ok=True)
        jp = os.path.join(args.out, "scan.json")
        with open(jp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"✅ 原始数据已保存：{jp}")

    if cmd in ("render", "all"):
        jp = getattr(args, "in", None) or os.path.join(args.out, "scan.json")
        if not os.path.exists(jp):
            print("❌ 找不到 scan.json，请先运行 collect。")
            sys.exit(1)
        with open(jp, encoding="utf-8") as fh:
            data = json.load(fh)
        png, txt = render(data, args.out)
        print(f"✅ 仪表盘图片：{png}")
        print(f"✅ 人话报告：{txt}")

    if cmd == "manual":
        partial = None
        if args.json_file:
            with open(args.json_file, encoding="utf-8") as fh:
                partial = json.load(fh)
        elif args.text:
            partial = _parse_user_text(args.text)
        else:
            partial = _interactive_manual()
        data = build_user_data(partial)
        os.makedirs(args.out, exist_ok=True)
        jp = os.path.join(args.out, "scan.json")
        with open(jp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        png, txt = render(data, args.out)
        print(f"✅ 原始数据已保存：{jp}")
        print(f"✅ 仪表盘图片：{png}")
        print(f"✅ 人话报告：{txt}")

    print("✅ 完成（全程本地，无数据外发）。")


if __name__ == "__main__":
    main()
