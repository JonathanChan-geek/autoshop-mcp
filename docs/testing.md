# 测试

`python -m unittest discover -s tests -v` 默认运行合成离线测试。未配置原厂环境时，原生集成测试会明确显示 skipped。

完整原生验证需要两个自己的完整工程，各自包含可信 AutoShop 编译得到的 `Output.prg` 基准；第一个工程至少有一个可编辑 IL 块及一条独立 OUT 指令，并包含 `MAIN.dat` 配置文件。

```powershell
$env:AUTOSHOP_INSTALL_DIR = 'C:\Program Files (x86)\AutoShop'
$env:AUTOSHOP_TEST_FIXTURE = 'C:\PLC\baseline-a'
$env:AUTOSHOP_SECOND_FIXTURE = 'C:\PLC\baseline-b'
python -m unittest discover -s tests -v
```

运行前先构建 x86 host。测试始终复制工程，不修改输入工程，不连接 PLC。原生测试覆盖：

- 两个工程从空缓存重新编译，机器码与基准相同。
- 全部 LD 块逐个转换为 IL，机器码不变。
- 修改一条输出指令后机器码变化，配置保持不变。
- 错误指令被原厂编译器拒绝，即使原工程留有旧产物。
- 未知 DLL、缺少新产物、配置被意外修改时拒绝交付。
- 整工程转换工作流完成后，源程序块的 IL 分析覆盖完整。
- 一次批量修改/编译调用能生成新机器码；错误指令只留下诊断，未发布中间工程或 ZIP。

合成测试另覆盖多处修改的同时定位、重叠拒绝、跨文件失败不交付、注释/字符串中的伪地址排除、分析覆盖提示、导出期间源文件变化和 MCP 嵌套补丁参数。

不同工程未必满足每项测试的前提；缺少相应 IL 输出指令时，该项会跳过。检查测试输出中的跳过原因。公开仓库不分发原厂运行库和基准工程，所以 GitHub CI 只验证合成测试、MCP 通信与 host 构建。
