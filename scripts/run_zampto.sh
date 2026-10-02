#!/usr/bin/env bash
# 跑 zampto_auto.py 的外壳：起 Xvfb、可选录屏、跑脚本、收尾清代理。
#
# 为什么单独一个脚本：renew-kit 的 composite action 只接受一条 command，
# 而这里要先起后台进程（Xvfb / ffmpeg）再跑主脚本、最后收尸并写 $GITHUB_ENV。
# 塞进 action 的 command 里就是一行 `a & b; c; d; exit $?`，没法读也没法测。
#
# 退出码 = zampto_auto.py 的退出码（0=续期成功或跳过，1=需要人工处理）。
set -uo pipefail

DISPLAY_NUM="${ZAMPTO_DISPLAY:-:99}"
export DISPLAY="$DISPLAY_NUM"

Xvfb "$DISPLAY_NUM" -screen 0 1280x720x24 +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 2

RECORD_PID=""
if [ "${ENABLE_RECORDING:-false}" = "true" ]; then
  echo "Recording enabled"
  ffmpeg -f x11grab -video_size 1280x720 -framerate 10 -i "$DISPLAY_NUM" \
    -codec:v libx264 -preset ultrafast -crf 30 \
    -pix_fmt yuv420p -movflags +faststart /tmp/recording.mp4 \
    >/tmp/ffmpeg.log 2>&1 &
  RECORD_PID=$!
  sleep 2
fi

python zampto_auto.py
PYTHON_EXIT=$?

sleep 2
if [ -n "$RECORD_PID" ]; then
  kill "$RECORD_PID" 2>/dev/null
  wait "$RECORD_PID" 2>/dev/null
fi
kill "$XVFB_PID" 2>/dev/null

# CRITICAL: 清空代理环境变量，否则后续 Node 步骤（upload-artifact / checkout
# 清理）会拿 socks5h:// 去解析 —— undici 不支持，直接抛 "Invalid URL protocol"。
# 写空值到 $GITHUB_ENV，让后续步骤继承空串而不是那个 socks5h 地址。
if [ -n "${GITHUB_ENV:-}" ]; then
  {
    echo "ALL_PROXY="
    echo "HTTP_PROXY="
    echo "HTTPS_PROXY="
  } >>"$GITHUB_ENV"
fi

exit "$PYTHON_EXIT"
