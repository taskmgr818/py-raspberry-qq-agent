# bitnp-authz

`bitnp-authz/` 保存私有仓库 `https://git.bitnp.net/BITNP/bitnp-authz` 的 Python 包源码，
对应提交 `643da5d210aacfd18643862ffdc3e5ef442b63f7` 的 `python/` 目录，版本为 `1.0.0`。
包含原始包配置、说明、源码和测试；未修改上游实现，与 guild 使用同一版本的职务规则。

主项目通过 `tool.uv.sources` 引用本目录；Docker 在安装依赖前复制它，源码分发包也包含它。
构建不需要私有仓库凭据或访问 `git.bitnp.net`。

更新时，从有权限访问上游的环境获取选定提交，用其 `python/` 目录替换本快照，
更新此处记录及 `pyproject.toml` 中的版本约束，然后执行：

```bash
uv lock --default-index https://pypi.org/simple
uv sync --frozen
PYTHONPATH=third_party/bitnp-authz python -m unittest discover -s third_party/bitnp-authz/tests
uv run pytest -q
```

上游说明中指向包目录之外的链接请在原始仓库查看。
