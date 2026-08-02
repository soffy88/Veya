const vscode = require('vscode');

function runAgent() {
    vscode.window.showInformationMessage('Running Hicode Agent...');
    // Add your agent running logic here
}

function debugAgent() {
    vscode.window.showInformationMessage('Debugging Hicode Agent...');
    // Add your debugging logic here
}

function activate(context) {
    let disposableRun = vscode.commands.registerCommand('hicode.runAgent', runAgent);
    let disposableDebug = vscode.commands.registerCommand('hicode.debugAgent', debugAgent);

    context.subscriptions.push(disposableRun);
    context.subscriptions.push(disposableDebug);
}

exports.activate = activate;