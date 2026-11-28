# S5 BASE_LOADER RC2：三项修复与 QEMU 回归

日期：2026-11-28。结论：三项修复已实现，离线回归通过。RC2 已作为 [GitHub 预发布测试包](https://github.com/LumiMod/lumix-s5-v29-firmware/releases/tag/control-center-input-fix-20261128-rc2) 上传，尚未真机验证；RC1 保留供对照。发布的是原已验证二进制，以下校验值未变。

## 修复结果

| 问题 | RC1 可复现行为 | RC2 行为 |
| --- | --- | --- |
| 保存失败仍退出 | 两个驱动拒绝创建文件后，面板仍关闭 | 创建失败、写入错误或短写均留在当前行，显示红色“保存失败”；再次 SET 可重试，成功才关闭 |
| 左右误触动作行 | 左右键触发重置，或写配置并退出 | “恢复默认设置”“保存配置并退出”只接受 SET；方向键、摇杆及重复/释放均不执行动作 |
| 首次实体按键不恢复配置 | 实体消息绕过软件按键加载器，模块与配置不加载 | 在实体消息处理路径加载模块；原厂驱动就绪后首次有效按下可静默恢复配置，再转交原厂按键处理 |

底座增加 `0x064759BE -> ldr_raw_input_hook` 调用。0x58 字节原始状态和 source 参数原样转交 `0x06642D22`。自动加载只检查新按下字段，不在绘图或通用 UI 事件中访问 SD。

就绪检查执行原厂 `0x0668794A`，其三个合法驱动句柄地址直接来自该函数的原始指令。它读取驱动状态、验证模式与句柄，无文件操作和等待；本修复在至少一个驱动通过检查之后才进行路径解析和文件读取。观察到驱动就绪掩码变化会允许重试；稳定掩码下，缺失或损坏模块不随每个按键重复读取。Setup/Fn 仍可显式重试。同一就绪掩码下替换卡或模块、且期间未观察到未就绪状态时，需要通过 Setup/Fn 重试。

## 验证结果

1. **QEMU 20 组检查通过**：同一测试框架在 RC1 上重现三类问题的四个实例，然后验证 RC2 的动作限制、三类写入故障、恢复后重试、关闭按键尾事件、真正 SET 重置、原厂就绪校验、延迟就绪、B 路径加载、观察到的拔插状态变化、损坏模块 CRC 回退和加载器忙锁重试。
2. **原有 Unicorn 输入回归 14 组通过**：方向键、两组摇杆编码、长按、关闭与再打开、Fn 快捷条、原厂事件转发、CRC/ABI 拒绝和双卡回退。
3. **加载器/模块回归通过**：无模块、坏 CRC、A 错误回退 B、SD CUBE 解析、ISP 参数、OpenVG 绘制。该 Unicorn 脚本跳过 JPEG；完整图像处理由下一项 QEMU 测试覆盖。
4. **完整 6000×4000 图像处理通过**：执行实际 JPEG 前置钩子和 SD 逆色 LUT，检查两幅 24,064,000 字节 NV16 平面的全部像素与行填充。输出 Y/C 校验值与 RC1 完全一致。函数确实返回，SP 与 r4–r11 保持正确；不以运行到指令上限算通过。这是合成图像的 LUT 钩子测试，不包括 JPEG 编码器或实拍画质。
5. **QEMU 部分 RTOS 启动通过**：15 次绘制、1 次 EGL swap，停止于要求的首帧；显示“Please set the time zone”，与原厂、RC1 首帧逐文件相同。首帧 SHA-256：`441650dc9de75635c6e5009d8937594af0b4a85bb0a6f4dd93725f09cd885fc7`。
6. **错误提示绘制检查通过**：用实际新模块执行 OpenVG 绘图，回放保存错误状态，红色“保存失败”完整可读，无文字重叠。这是 Unicorn 绘图回放，不是真机照片。
7. **打包回读通过**：加密后逐组件解密比对、组件 SHA-256、容器 CRC32、容器修改范围检查通过。相对 RC1 的程序差异只在新增/重定位的钩子及加载器代码区；`07_postboot3.bin`、`20_osdover.bin` 与 RC1 一致。

QEMU 功能测试执行原厂消息接收器、真实补丁调用点、加载器和原厂 raw-to-UI 转换器。测试在 `0x06642D22` 边界记录参数，并跳过该函数依赖的原厂外围环境，接入 `0x0663EB5A`；文件系统、模式查询、ISP 与底层 UI 使用替身。就绪验证器本身没有替换，驱动状态由测试构造。采用显式 Normal RAM 映射，栈/寄存器返回检查覆盖每次调用。

启动模型使用 NAND/图形 HLE 和缺失外设隔离，未预填 SD 模块或面板状态。`menu_rendered=false`，没有到达完整相机主菜单。真机冷启动、SD 控制器/DMA 时序、ISP 输出和多核缓存行为仍待机身复测；不能据此断言整机无其他 bug。

## 候选包

- 底座：`S5___V29_BASE_LOADER_RC2.bin`，90,238,464 字节，SHA-256 `f94fca5d63ffba5a4ef2d6f55c8828b93b20f740ed586d9f102b8a402ffd0e5d`。
- 模块：`S5_MOD.BIN`，32,396 字节，SHA-256 `d44d1048329d2759a533868e9da0dae964fe711f922a86d55d691e6ef6562bbe`。
- 程序明文：SHA-256 `f922d4612b5fb1046483fc7a283701d1525b9c82996adf23a5086165e66b9434`。

三项修复需要更新本包的底座和模块。ABI 仍为 2，ABI 检查不会区分 RC1/RC2；请按包内校验值确认配套文件。按已有更新流程使用底座，SD 固件名为 `S5___V30.bin`，容器 BCD 仍为 `0x0300`，不提高版本来强推更新。`S5_MOD.BIN` 保持文件名，放 SD 根目录。构建输出在 `build_rc2/`，仓库根目录历史二进制不是本次产物。

## 重现命令

```sh
python3 tools/build_loader_and_mod.py --dec-dir /path/to/unpacked-v29 --out-dir build_rc2 --mod-out build_rc2/S5_MOD.BIN
python3 tools/verify_rc2_qemu.py --build-dir build_rc2 --baseline-dir build_input_fix --output build_rc2/qemu-regression/functions
python3 tools/check_rc1_qemu_boot.py --case candidate --build-dir build_rc2 --output build_rc2/qemu-regression/boot
python3 tools/verify_native_input.py --build-dir build_rc2
python3 tools/verify_loader_and_mod.py --build-dir build_rc2 --mod-bin build_rc2/S5_MOD.BIN --skip-jpeg
python3 tools/pack_firmware.py --patch-dir build_rc2 --out-bin build_rc2/S5___V29_BASE_LOADER_RC2.bin --version-bcd 0x0300
```

QEMU 脚本使用现有 `s5-emulator` 项目及工具链；Unicorn 脚本需要对应 Python 环境。输出目录必须尚不存在，以保留旧测试证据。完整机器日志保留于 `build_rc2/qemu-regression/`，候选包附摘要 JSON 与测试日志。

## 真机复测要点

先验证方向键、摇杆只操作面板；在重置/保存行按左右，设置和界面应不变。正常 SET 保存后重启，卡就绪时首次方向键应恢复设置且不弹面板。保存失败时应保持面板可见并提示失败，恢复写入条件后 SET 可成功保存退出。回放/返回退出后原厂菜单应恢复正常响应。
