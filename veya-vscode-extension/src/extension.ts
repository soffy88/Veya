import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

export function activate(context: vscode.ExtensionContext) {
    console.log('veya extension is now active!');
    
    // 注册命令
    let generateCode = vscode.commands.registerCommand('veya.generateCode', async () => {
        try {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showErrorMessage('没有活动的编辑器');
                return;
            }
            
            // 获取用户输入
            const description = await vscode.window.showInputBox({
                prompt: '请输入代码描述',
                placeHolder: '例如：创建一个计算两个数之和的函数'
            });
            
            if (!description) {
                return;
            }
            
            // 调用veya服务
            const result = await callHicodeService('generate', {
                description: description,
                language: getLanguageFromUri(editor.document.uri)
            });
            
            if (result.success) {
                // 插入生成的代码
                const edit = new vscode.WorkspaceEdit();
                const position = editor.selection.active;
                edit.insert(editor.document.uri, position, result.code);
                await vscode.workspace.applyEdit(edit);
                
                vscode.window.showInformationMessage('代码生成完成！');
            } else {
                vscode.window.showErrorMessage(`生成失败: ${result.error}`);
            }
        } catch (error) {
            vscode.window.showErrorMessage(`发生错误: ${error}`);
        }
    });
    
    let completeCode = vscode.commands.registerCommand('veya.completeCode', async () => {
        try {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                return;
            }
            
            // 获取当前行的代码
            const line = editor.document.lineAt(editor.selection.active.line);
            const prefix = line.text.substring(0, editor.selection.active.character);
            
            // 调用veya服务进行代码补全
            const result = await callHicodeService('complete', {
                prefix: prefix,
                language: getLanguageFromUri(editor.document.uri)
            });
            
            if (result.success && result.completions.length > 0) {
                // 显示补全选项
                const selected = await vscode.window.showQuickPick(result.completions, {
                    placeHolder: '选择补全选项'
                });
                
                if (selected) {
                    const edit = new vscode.WorkspaceEdit();
                    const position = editor.selection.active;
                    edit.replace(editor.document.uri, new vscode.Range(
                        position.line, 
                        position.character - prefix.length,
                        position.line, 
                        position.character
                    ), selected);
                    await vscode.workspace.applyEdit(edit);
                }
            }
        } catch (error) {
            vscode.window.showErrorMessage(`补全失败: ${error}`);
        }
    });
    
    let analyzeCode = vscode.commands.registerCommand('veya.analyzeCode', async () => {
        try {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                return;
            }
            
            // 获取选中的代码
            const selectedText = editor.document.getText(editor.selection);
            if (!selectedText) {
                vscode.window.showInformationMessage('请先选中要分析的代码');
                return;
            }
            
            // 调用veya服务进行代码分析
            const result = await callHicodeService('analyze', {
                code: selectedText,
                language: getLanguageFromUri(editor.document.uri)
            });
            
            if (result.success) {
                // 显示分析结果
                const panel = vscode.window.createWebviewPanel(
                    'veyaAnalysis',
                    'veya 代码分析',
                    vscode.ViewColumn.Beside,
                    {}
                );
                
                panel.webview.html = `
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <title>代码分析结果</title>
                    </head>
                    <body>
                        <h2>代码分析结果</h2>
                        <pre>${JSON.stringify(result, null, 2)}</pre>
                    </body>
                    </html>
                `;
            } else {
                vscode.window.showErrorMessage(`分析失败: ${result.error}`);
            }
        } catch (error) {
            vscode.window.showErrorMessage(`分析失败: ${error}`);
        }
    });
    
    let collaborateTask = vscode.commands.registerCommand('veya.collaborateTask', async () => {
        try {
            // 显示协作任务界面
            const taskDescription = await vscode.window.showInputBox({
                prompt: '输入协作任务描述',
                placeHolder: '例如：实现用户登录功能'
            });
            
            if (!taskDescription) {
                return;
            }
            
            // 创建任务
            const result = await callHicodeService('collaborate', {
                action: 'create',
                description: taskDescription
            });
            
            if (result.success) {
                vscode.window.showInformationMessage(`协作任务已创建: ${result.taskId}`);
            } else {
                vscode.window.showErrorMessage(`创建任务失败: ${result.error}`);
            }
        } catch (error) {
            vscode.window.showErrorMessage(`协作任务失败: ${error}`);
        }
    });
    
    context.subscriptions.push(generateCode);
    context.subscriptions.push(completeCode);
    context.subscriptions.push(analyzeCode);
    context.subscriptions.push(collaborateTask);
}

export function deactivate() {}

// 调用veya服务的函数
async function callHicodeService(endpoint: string, params: any): Promise<any> {
    try {
        // 这里应该调用本地运行的veya服务
        // 为了演示，我们返回模拟数据
        
        // 实际实现中会发送HTTP请求到本地veya服务
        // const response = await fetch(`http://localhost:8000/api/v1/veya/${endpoint}`, {
        //     method: 'POST',
        //     headers: {
        //         'Content-Type': 'application/json'
        //     },
        //     body: JSON.stringify(params)
        // });
        
        // 模拟响应
        switch(endpoint) {
            case 'generate':
                return {
                    success: true,
                    code: `def ${params.description.replace(/\s+/g, '_')}():\n    """${params.description}"""\n    pass\n`
                };
            case 'complete':
                return {
                    success: true,
                    completions: [
                        'def function_name():',
                        'def function_name(parameters):',
                        'class ClassName:',
                        'if condition:',
                        'for item in iterable:'
                    ]
                };
            case 'analyze':
                return {
                    success: true,
                    analysis: {
                        complexity: 'low',
                        issues: [],
                        suggestions: ['考虑添加类型注解']
                    }
                };
            case 'collaborate':
                return {
                    success: true,
                    taskId: 'task_' + Math.random().toString(36).substr(2, 9)
                };
            default:
                return { success: false, error: '未知端点' };
        }
    } catch (error) {
        return { success: false, error: error.toString() };
    }
}

// 根据文件URI获取语言
function getLanguageFromUri(uri: vscode.Uri): string {
    const lang = uri.path.split('.').pop();
    switch(lang) {
        case 'py': return 'python';
        case 'js': return 'javascript';
        case 'ts': return 'typescript';
        case 'java': return 'java';
        case 'cpp': return 'cpp';
        case 'go': return 'go';
        case 'rs': return 'rust';
        default: return 'python';
    }
}