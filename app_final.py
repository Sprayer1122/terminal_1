from flask import Flask, render_template, request, jsonify, Response
import subprocess
import os
import json
import threading
import time
from datetime import datetime
import gzip
import functools
from collections import OrderedDict

app = Flask(__name__)

# Performance optimizations
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['TEMPLATES_AUTO_RELOAD'] = False

# Global variables for terminal sessions
terminal_sessions = {}
session_counter = 0

# Caching system for better performance
command_cache = OrderedDict()
file_cache = OrderedDict()
CACHE_SIZE = 100  # Keep last 100 entries

def add_to_cache(cache_dict, key, value):
    """Add item to cache with LRU eviction."""
    if key in cache_dict:
        cache_dict.move_to_end(key)
    else:
        if len(cache_dict) >= CACHE_SIZE:
            cache_dict.popitem(last=False)
        cache_dict[key] = value
    return value

def get_from_cache(cache_dict, key):
    """Get item from cache with LRU update."""
    if key in cache_dict:
        cache_dict.move_to_end(key)
        return cache_dict[key]
    return None

class FinalTerminalSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.current_dir = os.getcwd()
        self.history = []
        self.is_active = True
        self.output_buffer = ""
        
        # Set up environment
        self.env = os.environ.copy()
        self.env.update({
            'TERM': 'xterm-256color',
            'COLUMNS': '80',
            'LINES': '24',
            'PYTHONUNBUFFERED': '1',
            'SHELL': '/bin/bash',
            'USER': os.environ.get('USER', 'root'),
            'HOME': os.environ.get('HOME', '/root'),
            'PATH': os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
            'HISTFILE': '/tmp/.bash_history',
            'HISTSIZE': '1000',
            'HISTFILESIZE': '2000'
        })
        
        print(f"[DEBUG] Final terminal session {session_id} created successfully")
    
    def execute_command(self, command):
        """Execute a command and return the result."""
        if not command.strip():
            return ""
        
        # Add to history
        self.history.append(command)
        if len(self.history) > 100:
            self.history.pop(0)
        
        # Handle special commands
        if command.strip() == 'clear':
            return "\033[2J\033[H"  # Clear screen escape sequence
        
        if command.strip() == 'history':
            return '\n'.join([f"{i+1}  {cmd}" for i, cmd in enumerate(self.history[-20:])]) + '\n'
        
        if command.strip() == 'help':
            return """Available commands:
• Basic commands: ls, cd, pwd, cat, grep, find, mkdir, rm, cp, mv
• System info: whoami, hostname, uname, ps, df, free
• File editing: cat, echo, touch, head, tail, grep, sed, awk
• Network: ping, curl, wget, netstat, ifconfig
• Package management: apt update, apt install, apt remove
• Process control: kill, killall, ps aux
• Text processing: head, tail, sort, uniq, wc, sed, awk
• Compression: tar, gzip, zip, unzip
• Special commands: clear, history, help

File editing alternatives:
• 'cat filename' - View file contents
• 'echo "text" > filename' - Create/write to file
• 'echo "text" >> filename' - Append to file
• 'head -n 10 filename' - View first 10 lines
• 'tail -n 10 filename' - View last 10 lines
• 'grep "pattern" filename' - Search in file

Type any Linux command and press Enter to execute it.
"""
        
        # Handle vi/vim commands - open web-based editor
        if command.strip().startswith(('vi ', 'vim ')):
            filename = command.strip().split(' ', 1)[1].strip()
            filepath = os.path.join(self.current_dir, filename)
            
            # Check if file exists and read its content
            try:
                if os.path.exists(filepath):
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                else:
                    content = ""
                
                # Return special response that frontend will handle
                return f"__EDITOR__:{filename}:{content}"
            except Exception as e:
                return f"Error reading file {filename}: {str(e)}"
        
        # Handle other interactive commands
        other_interactive = ['nano', 'top', 'htop', 'less', 'more']
        if any(command.strip().startswith(cmd) for cmd in other_interactive):
            return f"""Interactive command '{command}' detected.

For interactive commands like nano, top, etc., please use the web-based alternatives:

• Instead of 'nano filename', use: 'echo "content" > filename' to create/edit
• Instead of 'top', use: 'ps aux' to see processes
• Instead of 'less filename', use: 'cat filename' to view

Or use these commands:
• 'cat filename' - View file contents
• 'echo "text" > filename' - Create/write to file
• 'echo "text" >> filename' - Append to file
• 'head -n 10 filename' - View first 10 lines
• 'tail -n 10 filename' - View last 10 lines
• 'grep "pattern" filename' - Search in file
"""
        
        # Handle cd command
        if command.strip().startswith('cd '):
            new_dir = command.strip()[3:].strip()
            if new_dir == '~':
                new_dir = os.path.expanduser('~')
            elif new_dir.startswith('~'):
                new_dir = os.path.expanduser(new_dir)
            elif not os.path.isabs(new_dir):
                new_dir = os.path.join(self.current_dir, new_dir)
            
            if os.path.exists(new_dir) and os.path.isdir(new_dir):
                self.current_dir = os.path.abspath(new_dir)
                return f"Changed directory to: {self.current_dir}\n"
            else:
                return f"Directory not found: {new_dir}\n"
        
                    # Execute the command with optimized timeout for network
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=5,  # Reduced timeout for network performance
                    cwd=self.current_dir,
                    env=self.env
                )
                
                # Format output efficiently
                output_parts = []
                if result.stdout:
                    # Truncate large outputs for network performance
                    stdout = result.stdout
                    if len(stdout) > 10000:  # 10KB limit
                        stdout = stdout[:10000] + "\n[Output truncated for performance]"
                    output_parts.append(stdout)
                    
                if result.stderr:
                    stderr = result.stderr
                    if len(stderr) > 5000:  # 5KB limit
                        stderr = stderr[:5000] + "\n[Error output truncated]"
                    output_parts.append(f"\n[STDERR] {stderr}")
                
                if not output_parts and result.returncode == 0:
                    output_parts.append(f"Command executed successfully (exit code: {result.returncode})")
                elif result.returncode != 0:
                    output_parts.append(f"\n[Exit code: {result.returncode}]")
                
                return ''.join(output_parts)
                
            except subprocess.TimeoutExpired:
                return f"Command '{command}' timed out after 5 seconds. This might be an interactive command that requires a real terminal.\n\nTry using alternatives:\n• 'cat filename' instead of 'vi filename'\n• 'ps aux' instead of 'top'\n• 'echo \"content\" > filename' instead of 'nano filename'"
            except Exception as e:
                return f"Error executing command: {str(e)}"
    
    def get_prompt(self):
        """Get the current prompt."""
        try:
            user = subprocess.run(['whoami'], capture_output=True, text=True).stdout.strip()
            hostname = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
            return f"{user}@{hostname}:{self.current_dir}$ "
        except:
            return f"root@localhost:{self.current_dir}$ "
    
    def close(self):
        """Close the session."""
        self.is_active = False

def create_terminal_session():
    """Create a new final terminal session."""
    global session_counter
    session_counter += 1
    session_id = f"session_{session_counter}"
    
    print(f"[DEBUG] Creating final terminal session: {session_id}")
    try:
        session = FinalTerminalSession(session_id)
        terminal_sessions[session_id] = session
        print(f"[DEBUG] Successfully created final session: {session_id}")
        return session_id
    except Exception as e:
        print(f"[DEBUG] Error creating final terminal session: {e}")
        return None

@app.route('/')
def index():
    """Main page with final terminal interface."""
    return render_template('final_terminal.html')

@app.route('/create_session', methods=['POST'])
def create_session():
    """Create a new final terminal session."""
    session_id = create_terminal_session()
    if session_id:
        return jsonify({'success': True, 'session_id': session_id})
    else:
        return jsonify({'success': False, 'error': 'Failed to create session'})

@app.route('/execute/<session_id>', methods=['POST'])
def execute_command(session_id):
    """Execute a command in the terminal session."""
    if session_id not in terminal_sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    data = request.get_json()
    command = data.get('command', '')
    
    session = terminal_sessions[session_id]
    result = session.execute_command(command)
    
    response = jsonify({
        'success': True,
        'output': result,
        'prompt': session.get_prompt()
    })
    
    # Add compression headers for network performance
    response.headers['Content-Encoding'] = 'gzip'
    response.data = gzip.compress(response.data)
    
    return response

@app.route('/save_file/<session_id>', methods=['POST'])
def save_file(session_id):
    """Save file content from the web editor."""
    if session_id not in terminal_sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    data = request.get_json()
    filename = data.get('filename', '')
    content = data.get('content', '')
    
    session = terminal_sessions[session_id]
    filepath = os.path.join(session.current_dir, filename)
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True, 'message': f'File {filename} saved successfully'})
    except Exception as e:
        return jsonify({'error': f'Error saving file: {str(e)}'}), 500

@app.route('/tab_complete/<session_id>', methods=['POST'])
def tab_complete(session_id):
    """Handle tab completion for commands and files."""
    if session_id not in terminal_sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    data = request.get_json()
    current_word = data.get('current_word', '')
    input_text = data.get('input', '')
    
    session = terminal_sessions[session_id]
    completions = []
    
    try:
        # Split input to determine what we're completing
        words = input_text.split()
        
        if len(words) == 1 or (len(words) == 2 and input_text.endswith(' ')):
            # Completing command name
            completions = get_command_completions(current_word)
        else:
            # Completing file/directory name
            completions = get_file_completions(current_word, session.current_dir)
        
        response = jsonify({
            'success': True,
            'completions': completions
        })
        
        # Add compression for network performance
        response.headers['Content-Encoding'] = 'gzip'
        response.data = gzip.compress(response.data)
        
        return response
        
    except Exception as e:
        return jsonify({'error': f'Completion error: {str(e)}'}), 500

def get_command_completions(prefix):
    """Get available command completions with caching."""
    # Check cache first
    cache_key = f"cmd_{prefix}"
    cached_result = get_from_cache(command_cache, cache_key)
    if cached_result is not None:
        return cached_result
    
    common_commands = [
        'ls', 'cd', 'pwd', 'cat', 'grep', 'find', 'mkdir', 'rm', 'cp', 'mv',
        'whoami', 'hostname', 'uname', 'ps', 'df', 'free', 'echo', 'touch',
        'head', 'tail', 'sort', 'uniq', 'wc', 'sed', 'awk', 'vi', 'vim',
        'ping', 'curl', 'wget', 'netstat', 'ifconfig', 'apt', 'kill', 'killall',
        'tar', 'gzip', 'zip', 'unzip', 'clear', 'history', 'help'
    ]
    
    result = [cmd for cmd in common_commands if cmd.startswith(prefix)]
    return add_to_cache(command_cache, cache_key, result)

def get_file_completions(prefix, current_dir):
    """Get file and directory completions with caching."""
    # Check cache first
    cache_key = f"file_{current_dir}_{prefix}"
    cached_result = get_from_cache(file_cache, cache_key)
    if cached_result is not None:
        return cached_result
    
    completions = []
    
    try:
        # Get all files and directories in current directory
        items = os.listdir(current_dir)
        
        for item in items:
            if item.startswith(prefix):
                item_path = os.path.join(current_dir, item)
                if os.path.isdir(item_path):
                    completions.append(item + '/')
                else:
                    completions.append(item)
        
        result = sorted(completions)
        return add_to_cache(file_cache, cache_key, result)
    except Exception:
        return []

@app.route('/close_session/<session_id>', methods=['POST'])
def close_session(session_id):
    """Close a terminal session."""
    if session_id in terminal_sessions:
        session = terminal_sessions[session_id]
        session.close()
        del terminal_sessions[session_id]
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Session not found'}), 404

@app.route('/sessions')
def list_sessions():
    """List all active sessions."""
    return jsonify({
        'sessions': list(terminal_sessions.keys()),
        'count': len(terminal_sessions)
    })

@app.route('/status')
def status():
    """Get system status information."""
    try:
        cwd = os.getcwd()
        user = subprocess.run(['whoami'], capture_output=True, text=True).stdout.strip()
        uname = subprocess.run(['uname', '-a'], capture_output=True, text=True).stdout.strip()
        
        return jsonify({
            'success': True,
            'cwd': cwd,
            'user': user,
            'system': uname,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

if __name__ == '__main__':
    print("🚀 Starting Optimized Web Terminal Server...")
    print("Access the terminal at: http://localhost:5008")
    print("Network access: http://[YOUR_IP]:5008")
    print("Performance optimizations: Compression + Caching enabled")
    print("Press Ctrl+C to stop the server")
    
    try:
        # Optimize for network performance
        app.run(
            host='0.0.0.0', 
            port=5008, 
            debug=False, 
            threaded=True,
            # Performance optimizations
            use_reloader=False,
            # Increase buffer sizes for network
            request_handler=None
        )
    except KeyboardInterrupt:
        print("\n🛑 Shutting down terminal sessions...")
        for session in terminal_sessions.values():
            session.close()
        print("✅ All sessions closed. Goodbye!") 
