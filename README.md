# Due Diligence Agent

Google ADK 企业公开来源初查助手。当前版本提供可交互的调查代理和 CLI，会话保存在当前进程内。

## 启动

在项目根目录用 Python 3.11 创建并同步虚拟环境。

```bash
uv sync --locked
uv run --locked python -m due_diligence.cli --trace
```

实时运行需要通过环境提供 `GEMINI_API_KEY` 或 `GOOGLE_API_KEY`。也可用 `uv run --env-file .env` 加载本地凭据。默认模型是 `gemini-3.1-flash-lite`，可通过 `--model` 或 `DUE_DILIGENCE_MODEL` 指定其他支持 Google Search 和 URL Context 的 Gemini 模型。

交互模式保留本次调查的前文。用 `/reset` 开始新案件，用 `/quit` 退出。CLI 和代理答复使用英文。以下三条可在同一会话中依次输入，检查能否直接从专题入手、随后复核并调整方向。

```text
Use the topic researcher to read https://www.iana.org/help/example-domains and explain what the example domains are for.
Review whether that source supports treating example.com as available for registration.
Switch to planning an ownership check for a public company. Ask me for the minimum identifiers you need.
```

也可以单轮运行或单独验证搜索。

```bash
uv run --locked python -m due_diligence.cli --trace --prompt \
  'Use read_public_url to read https://www.iana.org/help/example-domains and summarize it in one sentence.'

uv run --locked python -m due_diligence.cli --trace --preview-search --prompt \
  'Use Google Search to find public sources about IANA example domains.'
```

交互模式取得搜索结果后会提供一个临时本机浏览器链接，可查看来源和 Google 搜索建议。管道或批处理运行时，可加 `--preview-search` 保留链接，最长等待 30 秒。搜索结果供当前用户查看，原文读取使用独立确定的公开 URL 或用户给出的公开 URL。URL Context 仅在 Gemini 对指定地址返回抓取成功状态后将摘录交给代理。如果网站跳转后 Gemini 只报告最终地址，可以提供最终地址重试。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
```

自动化用例使用真实 ADK Runner、代理委派和工具配置，由脚本替代外部模型响应，因此无需 API 密钥。用例覆盖按用户意图从不同阶段开始、多轮改方向、专题代理阅读来源、搜索结果隔离、URL 校验和抓取状态。

## 后端入口

后续模块可直接调用 `ResearchAssistant.ask()` 获取答复、工具轨迹和当前轮的临时搜索结果。主代理根据用户意图调用主体识别、专题研究及结论复核代理。调查步骤由提示词提供指导，用户可以直接指定某个专题、先复核或返回初查。主代理可以生成和修改会话内的文字草稿，Word 导出属于后续报告模块。
