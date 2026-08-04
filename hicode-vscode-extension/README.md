# hicode VS Code Extension

基于hicode的AI编程助手VS Code插件，提供代码生成、补全和分析功能。

## 功能特性

- **代码生成**: 根据自然语言描述生成代码
- **智能补全**: 提供上下文相关的代码补全建议
- **代码分析**: 分析代码结构和质量
- **协作任务**: 管理多代理协作任务

## 安装

1. 在VS Code中打开扩展面板
2. 搜索"hicode"
3. 安装"hicode - AI编程助手"插件

## 使用方法

### 代码生成
1. 在编辑器中右键选择"hicode: 生成代码"
2. 或者在命令面板中输入"hicode: 生成代码"
3. 输入代码描述

### 代码补全
1. 在代码编辑器中输入代码片段
2. 插件会自动提供补全建议

### 代码分析
1. 选中代码片段
2. 右键选择"hicode: 分析代码"
3. 查看分析结果

## 配置

插件会自动连接到本地运行的hicode服务。

## 开发

### 构建插件
```bash
npm install
npm run compile
```

### 调试
1. 在VS Code中打开此项目
2. 按F5启动调试会话

## 支持的语言

- Python
- JavaScript/TypeScript
- Java
- C/C++
- Go
- Rust

## 许可证

MIT License