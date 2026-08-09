"""
Qwen3-TTS 1.7B CustomVoice 本地测试脚本
用法:
  交互模式:  python qwen_tts_test.py
  单次合成:  python qwen_tts_test.py Serena "主人你好呀，我是幻梦喵~"
  指定情绪:  python qwen_tts_test.py Vivian "哼，才不是为你呢" "用傲娇的语气说"

9 种音色:
  Vivian     - 明亮略带锋芒的年轻女声（中文，适合傲娇）
  Serena     - 温暖温柔的年轻女声（中文，适合猫娘幻梦）
  Uncle_Fu   - 低沉醇厚的成熟男声（中文）
  Dylan      - 北京年轻男声（北京话）
  Eric       - 成都年轻男声（四川话）
  Ryan       - 动感男声（英文）
  Aiden      - 阳光美国男声（英文）
  Ono_Anna   - 活泼日语女声（日语）
  Sohee      - 温暖韩语女声（韩语）

环境准备:
  conda create -n qwen3-tts python=3.12 -y
  conda activate qwen3-tts
  pip install -U qwen-tts modelscope soundfile torch
  modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice --local_dir ./models/Qwen3-TTS-12Hz-1.7B-CustomVoice
"""
import sys
import time
from pathlib import Path

import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

# 配置
_LOCAL = Path(__file__).resolve().parent.parent / "models" / "Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_PATH = str(_LOCAL) if _LOCAL.exists() else "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "tts_temp"

SPEAKERS = ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
            "Ryan", "Aiden", "Ono_Anna", "Sohee"]

DEFAULT_TEXT = "主人你好呀，我是幻梦喵~ 今天也要元气满满哦！"


def load_model():
    print(f"[TTS] 加载模型: {MODEL_PATH}")
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(
        MODEL_PATH,
        device_map="cuda:0",
        dtype=torch.float32,
    )
    print(f"[TTS] 模型加载完成 ({time.time()-t0:.1f}s)")
    try:
        speakers = model.get_supported_speakers()
        langs = model.get_supported_languages()
        print(f"[TTS] 支持音色: {speakers}")
        print(f"[TTS] 支持语言: {langs}")
    except Exception:
        pass
    return model


def synthesize(model, text, speaker="Serena", instruct=""):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out_file = OUT_DIR / f"tts_{ts}_{speaker}.wav"

    kwargs = dict(text=text, language="Chinese", speaker=speaker)
    if instruct:
        kwargs["instruct"] = instruct

    print(f"[TTS] 合成: speaker={speaker}, instruct={instruct or '(无)'}, text={text[:40]}...")
    t0 = time.time()
    try:
        wavs, sr = model.generate_custom_voice(**kwargs)
    except Exception as e:
        print(f"[TTS] 合成失败: {e}")
        return None

    sf.write(str(out_file), wavs[0], sr)
    print(f"[TTS] 已保存: {out_file} (sr={sr}, 耗时 {time.time()-t0:.1f}s)")
    return out_file


def interactive(model):
    print("\n=== 交互模式 ===")
    print("格式: <文本> | 或 <speaker> | <文本> | 或 <speaker> | <instruct> | <文本>")
    print("输入 q 退出，输入 l 列出音色\n")

    cur_speaker = "Serena"
    while True:
        try:
            line = input(f"[{cur_speaker}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        if line.lower() in ("l", "list"):
            for s in SPEAKERS:
                print(f"  {s}")
            continue

        parts = line.split("|")
        parts = [p.strip() for p in parts]
        text, instruct = "", ""
        if len(parts) == 1:
            text = parts[0]
        elif len(parts) == 2:
            if parts[0] in SPEAKERS:
                cur_speaker, text = parts[0], parts[1]
            else:
                instruct, text = parts[0], parts[1]
        elif len(parts) >= 3:
            if parts[0] in SPEAKERS:
                cur_speaker, instruct, text = parts[0], parts[1], parts[2]
            else:
                instruct, text = parts[0], parts[1]

        if not text:
            print("文本为空")
            continue
        synthesize(model, text, cur_speaker, instruct)


def main():
    model = load_model()

    if len(sys.argv) >= 3:
        speaker = sys.argv[1]
        if speaker not in SPEAKERS:
            print(f"未知音色: {speaker}，可选: {SPEAKERS}")
            return
        if len(sys.argv) >= 4:
            instruct = sys.argv[2]
            text = sys.argv[3]
        else:
            instruct = ""
            text = sys.argv[2]
        synthesize(model, text, speaker, instruct)
    else:
        interactive(model)


if __name__ == "__main__":
    main()
