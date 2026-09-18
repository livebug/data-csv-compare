---

## 三个文件怎么选

| 文件 | 目标机 | 要装 Python 吗 | 能改代码吗 |
| --- | --- | --- | --- |
| `...-windows-x64-standalone.zip` | Windows | **不要**，解压即用 | 不能 |
| `...-windows-x64-offline-kit.zip` | Windows | 不要（自带 Python 3.12 安装器） | **能**（含 wheels + 源码） |
| `...-wheels-src-py<版本>-<平台>.tar.gz` | Linux / Windows | **要**，版本与文件名一致 | **能**（含 wheels + 源码） |

- 名字里的 `py313` / `py312-313` 是**目标机需要自备的 Python 版本**；
  `linux-win` 是覆盖的平台。装的时候 pip 会按当前解释器自动挑对应的 wheel。
- 免 Python 的两个包同样支持 `compare` / `profile` / `gui` 全部子命令，参数与 Python 版一致。
- 每个包内都有 `MANIFEST.txt`：版本号、源码提交号、wheel 清单、生成时间，收到后先核对。
- 完整步骤（离线安装、二次开发、定时任务、glibc 兼容性）见仓库里的 `docs/DEPLOY.md`。
