#!/usr/bin/env python3
"""本地语音链 — P4（docs/pc-package-plan.md）。红线：ASR/TTS 永远本地（P2 设计钉死）。

  mic(ffmpeg avfoundation) → whisper-cli(zh + 简体偏置) → opencc t2s → 编排器
                          → piper(zh_CN-huayan-medium) → afplay/aplay

可选件：依赖/模型缺失时 raise VoiceUnavailable，编排器据此降级提示、文字路径零影响。
opencc 经 venv python 子进程转换（编排器本体保持 stdlib-only）。
板载实测结论沿用：whisper 选 base（中文零错字且最快）；whisper 中文出繁体 → t2s 后再进路由。

用法：python3 pc/voice.py --test                # roundtrip 自检（无需真人开麦）
      python3 pc/voice.py --speak "文本"         # 只说话
      python3 pc/voice.py --transcribe a.wav     # 只转写
"""
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def _ffmpeg_stderr_dump(proc):
    """ffmpeg 秒死时的死因转述（TCC 拒绝/无设备等）。"""
    try:
        _, err = proc.communicate(timeout=5)
    except Exception:
        return ""
    tail = (err or b"").decode(errors="replace").strip().splitlines()
    return " / ".join(tail[-2:]) if tail else ""

HOME_DIR = os.path.expanduser("~/.firela-pa")
GGML = os.environ.get("FIRELA_WHISPER_MODEL", f"{HOME_DIR}/models/ggml-base.bin")
VOICE = os.environ.get("FIRELA_PIPER_VOICE", f"{HOME_DIR}/voices/zh_CN-huayan-medium.onnx")
VENV_PY = os.environ.get("FIRELA_VENV_PY", f"{HOME_DIR}/venv/bin/python")
VENV_PIPER = os.environ.get("FIRELA_PIPER_BIN", f"{HOME_DIR}/venv/bin/piper")
# whisper 偏置：简体 + 财务领域词表（实测：贵州茅台→贵主摩台这类专名同音错字靠它救；
# initial prompt 参与解码，专有名词命中率显著提升）
BIAS_PROMPT = ("以下是关于财务、记账和股票行情的普通话简体句子，"
               "可能出现的词：贵州茅台、五粮液、平安银行、招商银行、腾讯、阿里巴巴、"
               "宁德时代、比亚迪、苹果、外卖、餐饮、净资产、涨了、跌了。")


class VoiceUnavailable(RuntimeError):
    pass


def _require():
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg 未安装（麦采/放音需要）")
    if not shutil.which("whisper-cli"):
        missing.append("brew install whisper.cpp")
    if not os.path.exists(GGML):
        missing.append(f"whisper 模型缺失 {GGML}")
    if not os.path.exists(VOICE):
        missing.append(f"piper 音色缺失 {VOICE}")
    if not os.path.exists(VENV_PIPER):
        missing.append("piper-tts 未安装")
    if missing:
        raise VoiceUnavailable("；".join(missing) + " —— bash install.sh --voice")


def _t2s(text):
    """whisper 中文常出繁体，路由模型简体训练——经 venv python 转简。"""
    r = subprocess.run([VENV_PY, "-c",
                        "import opencc,sys;print(opencc.OpenCC('t2s').convert(sys.argv[1]))",
                        text], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else text


def record_wav(path, max_seconds=30):
    """Enter 开始 / Enter 结束；单次 SIGINT 让 ffmpeg 收尾 wav 头（二次中断才损坏）。"""
    input("🎤 按 Enter 开始录音…")
    if sys.platform == "darwin":
        fmt = ["-f", "avfoundation", "-i", ":0"]
    else:
        fmt = ["-f", "pulse", "-i", "default"]
    # macOS avfoundation 实测（2026-09-19）：SIGINT/'q' 都要 ~28s 才让 ffmpeg 停（捕获线程
    # 堵主循环），且数据攒内存退出才落盘（裸 kill = 0 字节）。唯一快路径 =
    # -flush_packets 1 边录边写 + kill 立即停 + ffmpeg -c copy 毫秒级修 RIFF 头。全程 <0.1s。
    proc = subprocess.Popen(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", *fmt,
                             "-ar", "16000", "-ac", "1", "-t", str(max_seconds),
                             "-flush_packets", "1", path],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(1.2)
    if proc.poll() is not None:                      # 启动即死（TCC 拒绝/无输入设备）→ 当场报，不傻等 Enter
        raise RuntimeError(f"录音启动失败（多半是麦克风权限——系统设置→隐私与安全性→麦克风，"
                           f"给你的终端 App 打开）：{_ffmpeg_stderr_dump(proc)}")
    input("🔴 录音中…说完按 Enter 结束")
    print("⏳ …", end="", flush=True)
    proc.kill()                                      # 立即停（数据已在盘上）
    proc.wait()
    fixed = path + ".fixed.wav"
    r = subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", path, "-c", "copy", fixed],
                       capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode == 0 and os.path.exists(fixed):
        os.replace(fixed, path)
    else:
        os.path.exists(fixed) and os.unlink(fixed)


def transcribe(wav_path):
    _require()
    # -bs 1（贪心）实测 0.33s vs 默认 beam-5 的 23.8s（72×）；base 档的错字率两 setting 无差
    r = subprocess.run(["whisper-cli", "-m", GGML, "-f", wav_path, "-l", "zh",
                        "-nt", "-np", "-bs", "1", "--prompt", BIAS_PROMPT],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"whisper 失败：{r.stderr[-300:]}")
    text = r.stdout.strip()
    return _t2s(text) if text else ""


def speak(text):
    _require()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        r = subprocess.run([VENV_PIPER, "--model", VOICE, "--output_file", wav],
                           input=text, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"piper 失败：{r.stderr[-300:]}")
        player = "afplay" if sys.platform == "darwin" else "aplay"
        subprocess.run([player, wav], capture_output=True)
    finally:
        os.unlink(wav)


def _voice_rate():
    """音色采样率（huayan-medium=22050）——raw 管道播放需要。"""
    import json
    cfg = json.load(open(VOICE + ".json"))
    return int(cfg.get("audio", {}).get("sample_rate", 22050))


def speak_stream(text):
    """分句流式播报（调研结论 4）：piper --output_raw 原生按句渐进输出 PCM，
    直接管道给 ffplay——首句出声 ≈ piper 启动 + 首句合成（~0.3-0.5s），
    不等全文合成完。ffplay 缺席时回退整句文件路径。"""
    _require()
    if not shutil.which("ffplay"):
        return speak(text)
    piper = subprocess.Popen([VENV_PIPER, "--model", VOICE, "--output_raw"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    player = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
         "-f", "s16le", "-ar", str(_voice_rate()), "-ch_layout", "mono", "-i", "pipe:0"],
        # ffmpeg 8: -ac 已移除，raw 管道声道用 -ch_layout（实测 -ac 1 报 Option not found）
        stdin=piper.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    piper.stdout.close()                       # player 退出 → piper 收 SIGPIPE 而非僵死
    try:
        piper.stdin.write(text.encode())
    finally:
        piper.stdin.close()
    player.wait()
    piper.wait()


def listen():
    """录音 → 转写 → 简体文本（语音 REPL 的耳朵）。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        record_wav(wav)
        return transcribe(wav)
    finally:
        os.unlink(wav)


# ---------- filler 即时应声（#2：零 decode 零合成的「我看看」） ----------

FILLER_DIR = os.path.join(HOME_DIR, "filler")
FILLERS = ["我想想", "稍等，我查一下啊"]
_filler_i = 0


def warm_fillers():
    """预渲染 filler WAV——首次语音模式一次合成，之后每问纯文件回放。"""
    os.makedirs(FILLER_DIR, exist_ok=True)
    for i, text in enumerate(FILLERS):
        wav = os.path.join(FILLER_DIR, f"filler-{i}.wav")
        if os.path.exists(wav):
            continue
        r = subprocess.run([VENV_PIPER, "--model", VOICE, "--output_file", wav],
                           input=text, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            os.path.exists(wav) and os.unlink(wav)        # 残半文件会让 warm 永远跳过重合成
            raise RuntimeError(f"filler 合成失败：{r.stderr[-200:]}")


def play_filler():
    """异步播 filler（回放启动 ~几十 ms，满足说完→第一声 ≤0.7s）。返回句柄供抢停。"""
    global _filler_i
    wav = os.path.join(FILLER_DIR, f"filler-{_filler_i % len(FILLERS)}.wav")
    _filler_i += 1
    if not os.path.exists(wav):                          # warm 缺席时静默放弃，不拦作答
        return None
    player = "afplay" if sys.platform == "darwin" else "aplay"
    return subprocess.Popen([player, wav],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_filler(proc):
    """答案就绪即抢停残余 filler（通常 ~1s 片语早播完了，这里只是兜底防叠音）。"""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=1)                      # kill 后同样要收尸，防僵尸
            except Exception:
                pass


def roundtrip_test():
    """自检（无需真人）：piper 合成 → whisper 转写 → t2s → 归一化相似断言。"""
    _require()
    phrase = "这个月外卖花了多少"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        t0 = time.time()
        r = subprocess.run([VENV_PIPER, "--model", VOICE, "--output_file", wav],
                           input=phrase, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-200:]
        t_tts = time.time() - t0
        t1 = time.time()
        heard = transcribe(wav)
        t_asr = time.time() - t1
    finally:
        os.unlink(wav)

    norm = lambda s: re.sub(r"[，。？！、,.\s]", "", s)
    ratio = difflib.SequenceMatcher(None, norm(phrase), norm(heard)).ratio()
    print(f"TTS  {t_tts:.2f}s 合成: {phrase}")
    print(f"ASR  {t_asr:.2f}s 转写: {heard!r}")
    print(f"相似度 {ratio:.2f}（阈值 0.80）")
    assert ratio >= 0.80, f"roundtrip 相似度不足：{heard!r}"
    print("voice roundtrip: OK")


if __name__ == "__main__":
    if "--test" in sys.argv:
        roundtrip_test()
    elif "--speak" in sys.argv:
        speak(sys.argv[sys.argv.index("--speak") + 1])
    elif "--transcribe" in sys.argv:
        print(transcribe(sys.argv[sys.argv.index("--transcribe") + 1]))
    else:
        print(__doc__)
