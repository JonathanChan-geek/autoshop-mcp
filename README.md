# AutoShop MCP

[![CI](https://github.com/JonathanChan-geek/autoshop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/JonathanChan-geek/autoshop-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**不用操作 AutoShop 窗口，通过 MCP 或命令行读取、修改、转换和编译 H3U PLC 工程。**

本项目提供 Python 工具层和一个独立的 x86 原生进程。原生进程调用本机 AutoShop 的编译、转换 DLL，不启动 AutoShop 主程序，不模拟鼠标键盘。适合接入 AI 编程工具，也可以直接写脚本调用。

**0.3.0：新增 7 个工具，共 16 个。** 可以批量改多个程序块、一次完成编译打包、把整个工程转成 IL，并查询信号在各程序段的引用。新增静态检查和审查导出；不将静态结果当作动作仿真。

目前适配一个经过验证的 H3U 运行库组合，**不是通用 AutoShop SDK**。是否兼容以 DLL 的 SHA-256 为准，不能只看安装目录或软件版本号。原厂 DLL、安装包和现场 PLC 工程均不随本项目发布。

## 能做什么

| 工具 | 作用 |
| --- | --- |
| `capabilities` | 查看支持范围、工具参数和原生运行库状态 |
| `project_inspect` | 读取工程索引、程序块、文件类型和文件哈希 |
| `il_read` | 读取受支持的未加密 IL 指令文本 |
| `il_patch_copy` | 按原文件哈希和精确文本修改 IL，生成新工程副本 |
| `project_diff` | 比较两个工程的源码、配置和其他文件 |
| `package_project` | 打包离线工程，排除可能过期的编译产物 |
| `native_compile_probe` | 静态检查 PE 文件和导出，不执行 DLL |
| `native_compile_copy` | 调用原厂编译器，生成经过检查的新工程和 ZIP |
| `ld_to_il_copy` | 原厂 LD → IL 转换，转换前后机器码一致才交付副本 |
| `project_convert_all_copy` | 整工程 LD → IL，逐块验证等价后统一编译打包 |
| `il_batch_patch_copy` | 多文件、多处补丁同时校验，全部通过才生成副本 |
| `project_build_copy` | 可选转换、批量修改、原厂编译和打包的一次调用 |
| `project_search` | 搜索 IL 文本，返回文件、行号和上下文 |
| `project_xref` | 查询显式地址的引用位置和已识别指令的读写分类 |
| `project_audit` | 列出缺失文件、多处 OUT、跨程序块写入等复核线索 |
| `project_export` | 导出 UTF-8 指令文本、引用表、检查结果和哈希清单 |

暂不提供 ST、H5U、受保护工程的原生编译，也不提供连接 PLC、下载、运行或停止接口。编译成功说明通过了编译检查，不等于设备动作已完成现场验证。

## 安装

离线文件工具需要 Python 3.10 或更高版本。原生编译和 LD 转换还需要：

- Windows，以及自行安装的 AutoShop 和匹配的 VC90 MFC 运行库。
- Visual Studio 2022 Build Tools，安装“使用 C++ 的桌面开发”和 Windows SDK。
- 与 [profile.json](src/autoshop_mcp/native/profile.json) 完全一致的原厂 DLL。当前 profile 标识为 `h3u-4.10.2.4-local-1`；它是验证组合的标识，不代表支持所有同名版本。

在 PowerShell 中执行：

```powershell
git clone https://github.com/JonathanChan-geek/autoshop-mcp.git
cd autoshop-mcp
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 编译本项目的 x86 host；不需要复制原厂 DLL 到仓库。
.\src\autoshop_mcp\native\build.cmd

# 改成自己的安装目录。
$env:AUTOSHOP_INSTALL_DIR = 'C:\Program Files (x86)\AutoShop'
.\.venv\Scripts\python.exe -m autoshop_mcp capabilities
```

检查输出中的 `native_compile.available`。为 `false` 时，`mismatches` 会列出缺少或不匹配的文件；离线读取和修改工具仍可使用。

工具会在安装目录和 Windows WinSxS 中查找匹配的 `mfc90.dll`。需要指定检查路径时可设置 `AUTOSHOP_MFC_PATH`。该变量**不会改变 Windows 的 DLL 加载规则**；原生进程仍会检查实际加载的 MFC 哈希。不支持通过改哈希跳过版本校验，适配其他版本需要重新核对内部接口。

## 命令行用法

参数通过 UTF-8 JSON 文件传入。`project` 是包含 `.hcp` 和配套文件的工程目录，`dest` 必须是尚不存在的新目录。

例如，将以下内容保存为 `compile.json`：

```json
{
  "project": "C:/PLC/source",
  "dest": "C:/PLC/build-001"
}
```

然后编译：

```powershell
.\.venv\Scripts\python.exe -m autoshop_mcp native_compile_copy --params-file compile.json
```

成功结果包含 `ok: true`、`native_compiled: true`、机器码哈希和诊断信息。目标目录中包含 `project/`、`compiled-project.zip` 以及构建记录。失败时保留诊断，不把旧 `Output.prg` 当作新结果。

其他参数示例在 [examples](examples) 中。示例路径和指令仅供说明，使用前替换为自己的工程和修改内容。

常用流程：

1. `project_inspect` 确认工程和程序块。
2. 如果要改的是 LD，用 `ld_to_il_copy` 转换指定块。
3. `il_read` 取得指令和 SHA-256。
4. `il_patch_copy` 提交原哈希、精确旧文本和新文本，得到修改副本。
5. `project_diff` 核对范围，再用 `native_compile_copy` 编译修改副本。

`package_project` 只做离线打包，不表示原生编译成功。`native_compile_probe` 也只是静态检查，不能代替编译。

### 批量修改与一键编译

`il_batch_patch_copy` 和 `project_build_copy` 使用同一种补丁结构：

```json
{
  "project": "C:/PLC/source",
  "dest": "C:/PLC/build-002",
  "patches": [
    {
      "file": "MAIN.IL",
      "expected_sha256": "替换成 il_read 返回的 64 位 SHA-256",
      "edits": [
        {"old_text": "OUT\t\t Y1\n", "new_text": "OUT\t\t Y2\n"}
      ]
    }
  ]
}
```

每个文件列一次，`edits` 可列多处。所有替换都以**修改前的文本**定位，要求唯一匹配且区间不重叠。后面的替换不会意外命中前面新写入的内容。任一文件检查失败都不会交付部分修改的工程。

把参数保存后调用 `project_build_copy`，可直接得到编译包。只要修改副本时调用 `il_batch_patch_copy`。如果还需要转换，建议先调用 `project_convert_all_copy`，读取转换后 IL 的哈希，再准备补丁；`convert_all: true` 合并执行时也必须使用转换后的 IL 哈希。

### 查信号、查交叉写入

```json
{"project": "C:/PLC/all-il/project", "device": "Y2"}
```

将上述参数交给 `project_xref` 可查询 Y2 的显式引用。`project_audit` 查多处 OUT、跨块写入及缺失文件；`project_search` 可按字面文本找指令、常数或注释。

这些工具只分析已登记的未保护 IL，返回 `skipped_blocks` 和 `complete_source_coverage`。要读完整源程序，先转换全部 LD。地址引用**不展开双字隐含的相邻寄存器、批量范围、位地址和间接地址**；未知指令返回 `unknown`。没有搜索结果不能据此断言某个地址绝对未使用，多处写入也不一定表示逻辑错误。详细边界见 [能力说明](docs/capabilities.md)。

## 接入 MCP

将下面的配置加入支持 stdio MCP 的客户端，路径按实际安装位置修改：

```json
{
  "mcpServers": {
    "autoshop": {
      "command": "C:/tools/autoshop-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "autoshop_mcp", "serve"],
      "env": {
        "AUTOSHOP_INSTALL_DIR": "C:/Program Files (x86)/AutoShop"
      }
    }
  }
}
```

客户端只调用工具即可，不需要打开 AutoShop。服务使用当前用户的文件权限，应只连接自己信任的客户端。

## 编译结果怎么检查

- 原工程只读；原厂代码在临时工程副本和独立子进程中运行。
- 加载前校验 DLL 指纹；原生进程另行检查实际加载的运行库。
- 清除旧编译缓存，确认所有程序块都有本次生成的中间文件。
- 检查原厂错误信息、编译完成信号，以及连续两次一致的新机器码。
- 检查源文件和配置字节是否被编译器改动；异常时不交付工程包。
- LD → IL 转换会分别编译转换前后的工程，比较机器码是否逐字节一致。

实现与限制见 [原生后端说明](docs/native-backend.md)。

## 测试与贡献

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

公开测试使用代码生成的合成数据，覆盖文件处理、修改检查、打包和 MCP 通信；这些数据不是可下载到 PLC 的工程。GitHub Actions 在 Windows、Linux 上运行离线测试，并单独构建 x86 host。

原厂编译的集成测试需要你自己提供运行库和工程，默认跳过。配置方法见 [测试说明](docs/testing.md)。CI 通过不代表已在所有 AutoShop 版本或设备上验证。

欢迎提交 issue 和 PR。适配新版本时请附 DLL 指纹、复现步骤和验证结果，不要提交原厂二进制、客户工程或设备凭据。更多说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

本仓库原创代码采用 [MIT License](LICENSE)。AutoShop 及其原厂组件归各自权利人所有，需自行取得并遵守其许可；不属于本仓库 MIT 授权范围。本项目为独立社区项目，与汇川无官方隶属或背书关系。
