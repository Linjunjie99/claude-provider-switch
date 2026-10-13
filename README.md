# claude-provider-switch

`ccp`：切换 Claude Code 的 API 供应商。只改 `~/.claude/settings.json` 里的
`env.ANTHROPIC_BASE_URL` 和 `env.ANTHROPIC_AUTH_TOKEN`，其他内容一个字节都不动。
纯标准库，无运行时依赖。

## 安装

```sh
pipx install /root/workspace/20_personal/projects/claude-provider-switch
# 改代码后重新安装
pipx install --force /root/workspace/20_personal/projects/claude-provider-switch
```

## 用法

```sh
ccp save aws                                     # 把当前 settings.json 的地址和 token 存为供应商
ccp add other --url https://api.example.com      # 新增供应商，token 隐藏输入（或从 stdin 读一行）
ccp list                                         # * 为当前生效，token 打码显示
ccp use other                                    # 切换
ccp current                                      # 查看当前地址/token/匹配的供应商
ccp rm other                                     # 删除供应商文件（不改 settings.json）
```

修改某个供应商：`ccp add NAME --url URL --force`，之后再 `ccp use NAME`。

切换只对新开的 Claude Code 会话保证生效，已运行的会话请重启。

## 存储

- 供应商：`~/.claude/providers/<name>.json`（目录 0700，文件 0600，明文 token）。
  格式固定为 `{"env": {"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...}}`，
  出现其他键会拒绝使用。这个格式也能直接用于单次会话：
  `claude --settings ~/.claude/providers/<name>.json`。
- 备份：每次实际改动前把原文件原样复制到 `~/.claude/providers/.backups/`，保留最近 20 份。
  备份里含 settings.json 的全部内容（包括其他 key），权限 0600。
- 设置 `CLAUDE_CONFIG_DIR` 时，以上路径都以它为根目录。

## 写入保证

`ccp use` 的处理顺序：

1. 严格解析 settings.json：非法 JSON、重复键、NaN、顶层或 `env` 不是对象，一律拒绝，文件不动。
2. 发现 vim/emacs 锁文件（`.settings.json.swp` 等）时拒绝，避免编辑器保存时把改动盖回去；`--force` 跳过。
3. 目标已是当前值时什么都不写。
4. 两个键都存在时，只替换这两个值在原文本中的位置，缩进、换行、键顺序、手工排版都保留；
   缺键时追加到 `env` 末尾（这种情况会按原文件缩进重新排版）。
5. 写入前校验：去掉这两个键后，新旧文档的值和键顺序必须完全一致，否则放弃。
6. 先备份，再写临时文件并 fsync，替换前重读原文件，期间被别人改过就放弃；
   最后原子 rename。保留原文件权限、BOM、CRLF；settings.json 是软链接时写到链接目标。
7. 同一时间只允许一个 `ccp` 写入（`providers/.lock`）。

不处理的东西：`ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` 等其他键一律不碰；
若 `env` 里有 `ANTHROPIC_API_KEY` 或 shell 导出了相关变量，会给出提示。

## 开发

```sh
uv sync
uv run pytest
```
