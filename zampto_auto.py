name: 🔄 Zampto 自动续期 & 状态监控

on:
  schedule:
    - cron: '0 0 * * *' # 每天北京时间 08:00（UTC 00:00）
  workflow_dispatch:
    inputs:
      force_renew:
        description: '强制续期（忽略剩余天数）'
        required: false
        default: 'false'
        type: choice
        options:
          - 'false'
          - 'true'
      enable_recording:
        description: '录屏（true=录屏，false=不录屏）'
        required: false
        default: 'false'
        type: choice
        options:
          - 'false'
          - 'true'

jobs:
  zampto-auto:
    name: 检查状态 / 自动续期
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: 检出代码
        uses: actions/checkout@v4

      - name: 设置 Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: 安装系统依赖
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq \
            xvfb ffmpeg curl unzip \
            fonts-noto-cjk \
            libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
            libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
            libxfixes3 libxrandr2 libgbm1 \
            libasound2t64 || sudo apt-get install -y -qq libasound2

      - name: 安装 Python 依赖
        run: |
          pip install -r requirements.txt
          python -c "from cloakbrowser import ensure_binary; ensure_binary()"

      - name: 下载并启动 Hysteria2 代理
        env:
          HY2_CONFIG: ${{ secrets.HY2_CONFIG }}
        run: |
          curl -sL https://github.com/apernet/hysteria/releases/latest/download/hysteria-linux-amd64 -o hysteria
          chmod +x hysteria
          echo "$HY2_CONFIG" > config.yaml
          ./hysteria client -c config.yaml &
          sleep 5
          echo "直连 IP：$(curl -s --max-time 5 ifconfig.me || echo '失败')"
          echo "代理 IP：$(curl -s --max-time 10 --socks5 127.0.0.1:1080 ifconfig.me || echo '代理失败，请检查 HY2_CONFIG')"

      - name: 运行续期脚本
        env:
          ZAMPTO_USERNAME: ${{ secrets.ZAMPTO_USERNAME }}
          ZAMPTO_PASSWORD: ${{ secrets.ZAMPTO_PASSWORD }}
          ZAMPTO_SERVER_ID: ${{ secrets.ZAMPTO_SERVER_ID }}
          WXPUSHER_TOKEN: ${{ secrets.WXPUSHER_TOKEN }}
          WXPUSHER_UID: ${{ secrets.WXPUSHER_UID }}
          FORCE_RENEW: ${{ github.event.inputs.force_renew || 'false' }}
          ENABLE_RECORDING: ${{ github.event.inputs.enable_recording || 'false' }}
          DISPLAY: ":99"
          LIBGL_ALWAYS_SOFTWARE: "1"
          GALLIUM_DRIVER: "llvmpipe"
        run: |
          Xvfb :99 -screen 0 1280x720x24 +extension RANDR &
          XVFB_PID=$!
          sleep 2

          # 仅在 ENABLE_RECORDING=true 时启动录屏
          RECORD_PID=""
          if [ "$ENABLE_RECORDING" = "true" ]; then
            echo "🎬 录屏已启用"
            ffmpeg -f x11grab -video_size 1280x720 -framerate 10 -i :99 \
              -codec:v libx264 -preset ultrafast -crf 30 \
              -pix_fmt yuv420p -movflags +faststart /tmp/recording.mp4 \
              > /tmp/ffmpeg.log 2>&1 &
            RECORD_PID=$!
            sleep 2
          else
            echo "🎬 录屏未启用（如需录屏，手动触发时选择 enable_recording=true）"
          fi

          timeout 600 python zampto_auto.py || echo "脚本退出码: $?"

          sleep 2
          [ -n "$RECORD_PID" ] && kill $RECORD_PID 2>/dev/null || true
          [ -n "$RECORD_PID" ] && wait $RECORD_PID 2>/dev/null || true
          kill $XVFB_PID 2>/dev/null || true

      - name: 上传截图和录屏
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: debug-${{ github.run_number }}
          path: |
            ./screenshots/*.png
            ${{ github.event.inputs.enable_recording == 'true' && '/tmp/recording.mp4' || '' }}
            ${{ github.event.inputs.enable_recording == 'true' && '/tmp/ffmpeg.log' || '' }}
          if-no-files-found: ignore
          retention-days: 3

      - name: 清理旧运行记录（只保留最新 1 条）
        if: always()
        uses: Mattraks/delete-workflow-runs@v2
        with:
          token: ${{ github.token }}
          repository: ${{ github.repository }}
          delete_workflow_pattern: ${{ github.workflow }}
          retain_days: 0
          keep_minimum_runs: 1
