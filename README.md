# BiTop Global Lead Finder V1.2.1

本项目是 BiTop 用于海外汽车养护产品 B2B 客户发现与企业背调的本地运行工具。

## 核心功能
- Serper 搜索公开企业与渠道信息
- 本地 SQLite 客户库，支持筛选、分级、导入和 CSV 导出
- 公开网页读取与证据来源记录
- OpenAI Sign in with ChatGPT（OAuth + PKCE）授权
- 按当前账户授权展示模型，并通过 Responses API 进行 AI 企业背调
- 数据处理确认、来源校验、OAuth 凭据本地加密

## 安装与运行
1. 安装 Python 3.11 或更高版本。
2. 解压项目文件夹。
3. 双击 Windows 的 `start.bat`。
4. 打开 http://127.0.0.1:8765 。
5. 在应用中配置公开 GitHub 仓库 URL、Serper API Key，并按页面提示连接 ChatGPT。

依赖安装：`py -3 -m pip install -r requirements.txt`

## 数据与安全
- 客户数据保存在本机 `leads.sqlite3`，不会自动上传到本仓库。
- OAuth 凭据保存在本机 `chatgpt_auth.bin`；Windows 使用 DPAPI。
- Serper Key、令牌、配置、客户数据库和客户资料不得提交到 GitHub。
- AI 背调结果只是公开信息研究辅助，必须人工核验。
- 搜索和 AI 背调由用户主动触发；批量任务会显示请求及用量确认。

详见 [隐私与数据处理说明](privacy.html)、[MIT License](LICENSE) 和 [更新记录](CHANGELOG.txt)。

## 测试
```bash
python -m unittest discover -s tests -v
python -m py_compile app.py siwc.py
```

## 免责声明
本项目是独立开源客户端，不代表 OpenAI 或 Serper 官方产品或背书。Sign in with ChatGPT 预览规范、模型可用性和账户用量限制可能变化；使用前请阅读 OpenAI 官方条款及开发文档。
