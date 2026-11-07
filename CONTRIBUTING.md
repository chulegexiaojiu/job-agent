# 开发与验证

面向 Windows 11、Python 3.10+（含 Tcl/Tk）。本地运行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m compileall -q job_agent tools tests
.venv\Scripts\python -W error::ResourceWarning -m unittest discover -s tests -v
```

离线测试不需要 API Key，不访问招聘方。部分测试创建 Tk 窗口、真实隔离子进程和回环 HTTP 服务。GitHub Actions 配置运行 Windows 下 Python 3.10／3.12 的离线检查；配置存在不代表远端 CI 已通过。

改动浏览器交互时，在可用的本地桌面另行运行 `python -m tools.check_chat_ui` 和 `python -m tools.check_delivery_ui`。这会打开 Edge 中的本地合成页面；不要同时手动操作浏览器。测试不联系招聘方，不证明真实平台可用。

`python -m tools.check_workflow` 会用本机已保存的 API 连接执行联网验证并产生费用。它使用合成申请人及公开公司研究，不提交申请，不属于 CI。不要自动重试以掩盖核验失败。

新增外发行为必须保留默认关闭、明确站点白名单、精确页面与附件绑定、额度预占、跨渠道去重、最新回复核验、未知结果不重试和取消回收。不要通过放宽身份／来源校验来使测试通过。

新增测试只用合成数据。提交前审阅暂存内容，并运行：

```powershell
python -m tools.check_release
```

检查器只扫描 Git index，不能替代人工检查或历史扫描。报告和所有用户数据留在被忽略的 `private/`。详细边界见 `SECURITY.md`。
