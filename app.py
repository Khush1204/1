from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
from datetime import datetime
from werkzeug.utils import secure_filename
import uuid

# Configuration
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'jpg', 'jpeg', 'png', 'gif', 'mp3', 'mp4', 'zip', 'rar'}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB max file size

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Initialize SocketIO
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=True,
    engineio_logger=True
)

# In-memory storage (resets on server restart)
active_rooms = {}  # {room_id: {user_sid: username}}
messages = {}      # {room_id: [message1, message2, ...]}
file_uploads = {}  # Store file metadata temporarily

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Routes
@app.route('/')
def index():
    return render_template('index.html')

# File upload endpoint
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)
        
        # Store file metadata
        file_id = str(uuid.uuid4())
        file_uploads[file_id] = {
            'path': filepath,
            'filename': filename,
            'uploaded_at': datetime.now().isoformat()
        }
        
        return jsonify({
            'file_id': file_id,
            'filename': filename,
            'url': f'/uploads/{unique_filename}'
        })
    
    return jsonify({'error': 'File type not allowed'}), 400

# Serve uploaded files
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")
    # Clean up user from all rooms
    for room_id, users in list(active_rooms.items()):
        if request.sid in users:
            username = users[request.sid]
            del users[request.sid]
            
            # Notify room if there are still users
            if users:
                emit('user_left', {
                    'username': username,
                    'message': f'{username} has left the room',
                    'timestamp': datetime.now().isoformat(),
                    'users': list(users.values())
                }, room=room_id)
            else:
                # Clean up empty room
                if room_id in active_rooms:
                    del active_rooms[room_id]
                if room_id in messages:
                    del messages[room_id]

@socketio.on('join')
def on_join(data):
    username = data.get('username', '').strip()
    room_id = data.get('room_id', 'lobby')
    
    if not username or len(username) < 2 or len(username) > 20:
        return {'status': 'error', 'message': 'Username must be 2-20 characters'}
    
    # Create room if it doesn't exist
    if room_id not in active_rooms:
        active_rooms[room_id] = {}
        messages[room_id] = []
    
    # Handle duplicate usernames
    if username in active_rooms[room_id].values():
        return {'status': 'error', 'message': 'Username already taken'}
    
    # Add user to room
    active_rooms[room_id][request.sid] = username
    join_room(room_id)
    
    # Send join confirmation
    emit('join_confirmation', {
        'username': username,
        'room_id': room_id,
        'users': list(active_rooms[room_id].values()),
        'messages': messages[room_id][-50:]  # Last 50 messages
    })
    
    # Notify room
    emit('user_joined', {
        'username': 'System',
        'message': f'{username} has joined the room',
        'users': list(active_rooms[room_id].values()),
        'timestamp': datetime.now().isoformat()
    }, room=room_id, include_self=False)
    
    return {'status': 'success'}

@socketio.on('send_message')
def handle_message(data):
    room_id = data.get('room_id')
    message = data.get('message', '').strip()
    file_info = data.get('file')
    
    if room_id not in active_rooms or request.sid not in active_rooms[room_id]:
        return {'status': 'error', 'message': 'Not in room'}
    
    if not message and not file_info:
        return {'status': 'error', 'message': 'Message cannot be empty'}
    
    username = active_rooms[room_id][request.sid]
    message_data = {
        'id': str(uuid.uuid4()),
        'username': username,
        'message': message,
        'timestamp': datetime.now().isoformat(),
        'file': file_info
    }
    
    # Store message
    messages[room_id].append(message_data)
    
    # Broadcast to room
    emit('new_message', message_data, room=room_id)
    
    return {'status': 'success'}

# WebRTC signaling
@socketio.on('webrtc_offer')
def handle_offer(data):
    target_sid = data.get('target_sid')
    offer = data.get('offer')
    caller_username = active_rooms.get(data.get('room_id', ''), {}).get(request.sid, 'Someone')
    
    emit('webrtc_offer', {
        'offer': offer,
        'caller_sid': request.sid,
        'caller_username': caller_username
    }, room=target_sid)

@socketio.on('webrtc_answer')
def handle_answer(data):
    target_sid = data.get('target_sid')
    answer = data.get('answer')
    
    emit('webrtc_answer', {
        'answer': answer,
        'sender_sid': request.sid
    }, room=target_sid)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    target_sid = data.get('target_sid')
    candidate = data.get('candidate')
    
    emit('ice_candidate', {
        'candidate': candidate,
        'sender_sid': request.sid
    }, room=target_sid)

if __name__ == '__main__':
    # Get port from environment variable or use 5000 as default
    port = int(os.environ.get('PORT', 5000))
    # Run the SocketIO app
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
