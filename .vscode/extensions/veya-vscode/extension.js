/**
 * veya VS Code extension — G6 closed loop
 *
 *  发起任务 → 接受 SSE 流 → 应用文件改动 / 刷新工作区
 *
 * Flow:
 *  1. POST /vscode/run-stream  {persona, text} → {session_id}
 *  2. GET  /stream/{session_id}                → SSE data: events
 *     - session_start / squad_start / text_delta / squad_done / cost_update / task_done
 *  3. 事件流写入输出面板;task_done 后刷新文件资源管理器(引擎已直接落盘改动)。
 *
 * 设计:输出面板增量渲染;错误事件红色提示;无轮询。
 */
const vscode = require('vscode');
const http = require('http');
const https = require('https');

let currentSessionId = null;
let outputChannel = null;

/** @param {vscode.ExtensionContext} context */
function activate(context) {
	outputChannel = vscode.window.createOutputChannel('Hicode');
	context.subscriptions.push(outputChannel);

	const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
	statusBarItem.text = '$(sparkle) veya';
	statusBarItem.command = 'veya.quickChat';
	statusBarItem.show();
	context.subscriptions.push(statusBarItem);

	context.subscriptions.push(
		vscode.commands.registerCommand('veya.runAgent', () => runAgentCommand()),
		vscode.commands.registerCommand('veya.debugAgent', () => debugAgentCommand()),
		vscode.commands.registerCommand('veya.createSession', () => createSessionCommand()),
		vscode.commands.registerCommand('veya.quickChat', () => quickChatCommand())
	);
}

/** API base URL from settings. */
function apiBase() {
	return vscode.workspace.getConfiguration('veya').get('apiBaseUrl', 'http://localhost:8000');
}

/**
 * G6 核心:发起任务 → SSE 流式渲染 → 完成后刷新工作区。
 */
async function runAgentCommand() {
	const input = await vscode.window.showInputBox({
		prompt: 'Enter your request for the agent',
		placeHolder: 'e.g., "Create a REST API endpoint"',
		value: ''
	});
	if (!input) return;

	const persona = await vscode.window.showQuickPick(
		['plan', 'research', 'build', 'execute'],
		{ placeHolder: 'Select agent persona' }
	);
	if (!persona) return;

	outputChannel.show();
	outputChannel.appendLine(`\n=== ${persona.toUpperCase()} · ${input} ===`);

	try {
		const { session_id, stream_url } = await callApi('/vscode/run-stream', {
			persona,
			text: input,
			project: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
			session_id: currentSessionId
		}, 'POST');
		currentSessionId = session_id;

		const outcome = await consumeSSE(stream_url, (event) => {
			renderEvent(event, persona);
		});

		// 引擎已直接落盘改动;刷新文件资源管理器让改动可见(G6 应用文件改动)
		await vscode.commands.executeCommand('workbench.files.refresh');
		if (outcome === 'success') {
			vscode.window.showInformationMessage(`Hicode agent completed (session ${session_id.slice(0, 8)}…)`);
		} else {
			vscode.window.showWarningMessage(`Hicode agent finished with errors — see output panel.`);
		}
	} catch (err) {
		vscode.window.showErrorMessage(`Failed to run agent: ${err.message}`);
	}
}

/**
 * 消费 SSE 流。返回 'success' | 'error'。
 * @param {string} streamUrl 如 "/stream/abc"
 * @param {(event: object) => void} onEvent
 * @returns {Promise<string>}
 */
function consumeSSE(streamUrl, onEvent) {
	return new Promise((resolve, reject) => {
		const url = new URL(streamUrl, apiBase());
		const lib = url.protocol === 'https:' ? https : http;
		const req = lib.get(url, (res) => {
			if (res.statusCode !== 200) {
				reject(new Error(`SSE ${res.statusCode}: ${res.statusMessage}`));
				res.resume();
				return;
			}
			let buffer = '';
			let final = 'success';
			res.setEncoding('utf8');
			res.on('data', (chunk) => {
				buffer += chunk;
				const lines = buffer.split('\n');
				buffer = lines.pop() || '';
				for (const line of lines) {
					const trimmed = line.trim();
					if (!trimmed.startsWith('data:')) continue;
					const payload = trimmed.slice(5).trim();
					if (payload === '[DONE]') { resolve(final); return; }
					try {
						const event = JSON.parse(payload);
						if (event.type === 'task_error') final = 'error';
						onEvent(event);
					} catch (e) { /* ignore malformed */ }
				}
			});
			res.on('end', () => resolve(final));
		});
		req.on('error', reject);
		req.setTimeout(300000, () => req.destroy(new Error('SSE timeout')));
	});
}

/** 增量渲染单个事件到输出面板。 */
function renderEvent(event, persona) {
	switch (event.type) {
		case 'session_start':
			outputChannel.appendLine(`── session ${String(event.session_id).slice(0, 8)} started ──`);
			break;
		case 'squad_start':
			outputChannel.appendLine(`\n▶ [${event.role}] ${event.squad_id} …`);
			break;
		case 'text_delta':
			outputChannel.append(event.delta || '');
			break;
		case 'squad_done':
			outputChannel.appendLine(`\n◀ [${event.role}] ${event.status}${event.cost_usd ? ` · $${event.cost_usd.toFixed(4)}` : ''}`);
			break;
		case 'cost_update':
			outputChannel.appendLine(`\ncost so far: $${Number(event.total_cost).toFixed(4)}`);
			break;
		case 'task_done':
			outputChannel.appendLine(`\n${'─'.repeat(40)}\n${event.result || ''}`);
			break;
		case 'task_error':
			outputChannel.appendLine(`\n✖ ERROR: ${event.error || 'unknown'}`);
			break;
		default:
			break;
	}
}

async function debugAgentCommand() {
	if (!currentSessionId) {
		vscode.window.showWarningMessage('No active session. Create a session first.');
		return;
	}
	try {
		const result = await callApi(`/vscode/debug/start?session_id=${currentSessionId}`, {}, 'POST');
		vscode.window.showInformationMessage(
			`Debug session started for ${currentSessionId}\n` +
			`Breakpoints: ${result.breakpoints.length}`
		);
	} catch (error) {
		vscode.window.showErrorMessage(`Failed to start debug: ${error.message}`);
	}
}

async function createSessionCommand() {
	// 会话由 run-stream 自动创建;此命令保留以重置当前会话引用
	currentSessionId = null;
	vscode.window.showInformationMessage('Session reference reset — next run creates a fresh session.');
}

async function quickChatCommand() {
	const message = await vscode.window.showInputBox({
		prompt: 'Ask veya anything...',
		placeHolder: 'e.g., "How do I add a new route?"',
		value: ''
	});
	if (!message) return;

	outputChannel.show();
	try {
		const result = await callApi('/vscode/chat', { message, session_id: currentSessionId }, 'POST');
		outputChannel.appendLine(`\n[You]: ${message}`);
		outputChannel.appendLine(`[Hicode]: ${result.response || 'No response'}`);
		if (result.session_id && result.session_id !== currentSessionId) {
			currentSessionId = result.session_id;
		}
	} catch (error) {
		vscode.window.showErrorMessage(`Chat failed: ${error.message}`);
	}
}

/** 通用 JSON API 调用。 */
function callApi(path, body = {}, method = 'GET') {
	return new Promise((resolve, reject) => {
		const url = new URL(path, apiBase());
		const options = {
			method,
			headers: { 'Content-Type': 'application/json' }
		};
		const lib = url.protocol === 'https:' ? https : http;
		const req = lib.request(url, options, (res) => {
			let data = '';
			res.setEncoding('utf8');
			res.on('data', (chunk) => { data += chunk; });
			res.on('end', () => {
				try { resolve(JSON.parse(data)); }
				catch (e) { resolve({ raw: data }); }
			});
		});
		req.on('error', reject);
		req.setTimeout(120000, () => req.destroy(new Error('API timeout')));
		if (method !== 'GET' && Object.keys(body).length > 0) {
			req.write(JSON.stringify(body));
		}
		req.end();
	});
}

function deactivate() {}

module.exports = { activate, deactivate };
