import logging
from logging.config import dictConfig
from opcua import ua, Server
from flask import Flask, request, jsonify, render_template
import threading
import json
import os
import time

# Configure logging
dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'default': {
            'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
        },
    },
    'handlers': {
        'file': {
            'class': 'logging.FileHandler',
            'filename': 'server.log',
            'formatter': 'default',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'default',
        },
    },
    'root': {
        'level': 'INFO',
        'handlers': ['file', 'console']
    },
    'loggers': {
        'opcua': {
            'level': 'ERROR',
            'handlers': ['file', 'console'],
            'propagate': False,
        },
        'werkzeug': {
            'level': 'WARNING',
            'handlers': ['file', 'console'],
            'propagate': False,
        },
        'opcua_server': {
            'level': 'DEBUG',
            'handlers': ['file'],
            'propagate': True,
        },
    }
})

app = Flask(__name__)
opcua_server = None
simulation_threads = {}


class CustomOpcUaServer:
    def __init__(self, endpoint, uri):
        self.server = Server()
        self.server.set_endpoint(endpoint)
        self.server.set_server_name("Custom OPC UA Server")
        self.server.set_application_uri(uri)
        self.idx = self.server.register_namespace(uri)
        self.objects = self.server.get_objects_node()
        self.tags = {}
        self.server_running = False
        self.endpoint = endpoint
        self.uri = uri

        self.load_state()

    def load_state(self):
        if os.path.exists("server_state.json"):
            with open("server_state.json", "r") as file:
                state = json.load(file)
                for tag in state.get("tags", []):
                    self.add_tag(tag['name'], tag['type'], tag['value'], tag['writable'])

    def save_state(self):
        state = {
            "tags": [
                {
                    "name": name,
                    "type": tag.get_data_type_as_variant_type().name,
                    "value": tag.get_value(),
                    "writable": True
                }
                for name, tag in self.tags.items()
            ]
        }
        with open("server_state.json", "w") as file:
            json.dump(state, file)

    def add_tag(self, name, dtype, initial_value=None, writable=True):
        dtype_mapping = {
            'int': ua.VariantType.Int32,
            'int32': ua.VariantType.Int32,
            'float': ua.VariantType.Float,
            'string': ua.VariantType.String,
            'bool': ua.VariantType.Boolean,
        }

        dtype = dtype.lower()
        if dtype not in dtype_mapping:
            raise ValueError(f"Unsupported data type: {dtype}")

        variant_type = dtype_mapping[dtype]

        if dtype == 'int' or dtype == 'int32':
            initial_value = int(initial_value) if initial_value is not None else 0
        elif dtype == 'float':
            initial_value = float(initial_value) if initial_value is not None else 0.0
        elif dtype == 'bool':
            initial_value = bool(initial_value) if initial_value is not None else False

        var = self.objects.add_variable(self.idx, name, ua.Variant(initial_value, variant_type))

        if writable:
            var.set_writable()

        self.tags[name] = var
        self.save_state()
        return var

    def remove_tag(self, name):
        if name in self.tags:
            self.tags[name].delete()
            del self.tags[name]
            self.save_state()

    def remove_all_tags(self):
        for name in list(self.tags.keys()):
            self.remove_tag(name)
        self.save_state()

    def start(self):
        try:
            self.server.start()
            self.server_running = True
        except Exception as e:
            self.server_running = False
            logging.error(f"Failed to start OPC UA server: {e}")

    def stop(self):
        try:
            if self.server_running:
                self.server.stop()
                self.server_running = False
        except Exception as e:
            logging.error(f"Failed to stop OPC UA server: {e}")

    def start_simulation(self, name, dtype, min_val, max_val, gain, interval):
        def simulate():
            increasing = True
            current_value = min_val

            logging.info(f"Simulation started for {name}, initial value: {current_value}")

            while name in simulation_threads:
                if increasing:
                    current_value += gain
                    if current_value >= max_val:
                        current_value = max_val
                        increasing = False
                else:
                    current_value -= gain
                    if current_value <= min_val:
                        current_value = min_val
                        increasing = True

                if dtype in ['int', 'int32']:
                    current_value = int(current_value)
                elif dtype == 'float':
                    current_value = float(current_value)
                elif dtype == 'bool':
                    current_value = bool(current_value)

                logging.debug(f"Updating {name} to {current_value}")
                self.tags[name].set_value(ua.Variant(current_value, self.tags[name].get_data_type_as_variant_type()))
                time.sleep(interval)

        thread = threading.Thread(target=simulate)
        thread.daemon = True
        thread.start()
        simulation_threads[name] = thread


@app.route('/')
def index():
    if opcua_server is None or not opcua_server.server_running:
        return render_template('index.html', server_info=None, tags={})
    server_info = {
        "endpoint": opcua_server.endpoint,
        "uri": opcua_server.uri
    }
    return render_template('index.html', server_info=server_info, tags=opcua_server.tags)


@app.route('/start_server', methods=['POST'])
def start_server():
    global opcua_server
    endpoint = request.form.get('endpoint')
    uri = request.form.get('uri')
    opcua_server = CustomOpcUaServer(endpoint, uri)

    server_thread = threading.Thread(target=opcua_server.start)
    server_thread.start()
    server_thread.join()

    if opcua_server.server_running:
        return jsonify({"status": "Server started"})
    else:
        return jsonify({"status": "Server failed to start"}), 500


@app.route('/stop_server', methods=['POST'])
def stop_server():
    global opcua_server
    if opcua_server and opcua_server.server_running:
        opcua_server.stop()
        return jsonify({"status": "Server stopped"})
    return jsonify({"status": "Server is not running"}), 400


@app.route('/add_tag', methods=['POST'])
def add_tag():
    if opcua_server is None or not opcua_server.server_running:
        return jsonify({"status": "Server not started"}), 400

    name = request.form.get('name')
    dtype = request.form.get('dtype')
    initial_value = request.form.get('initial_value')
    writable = request.form.get('writable', 'true').lower() in ['true', '1', 'yes']

    if dtype == 'int':
        initial_value = int(initial_value) if initial_value else 0
    elif dtype == 'float':
        initial_value = float(initial_value) if initial_value else 0.0
    elif dtype == 'string':
        initial_value = str(initial_value) if initial_value else ""
    elif dtype == 'bool':
        initial_value = initial_value.lower() in ['true', '1', 'yes'] if initial_value else False

    try:
        opcua_server.add_tag(name, dtype, initial_value, writable)
        return jsonify({"status": f"Tag {name} added"})
    except Exception as e:
        logging.error(f"Error adding tag {name}: {e}")
        return jsonify({"status": f"Error adding tag {name}"}), 500


@app.route('/add_simulation_tag', methods=['POST'])
def add_simulation_tag():
    if opcua_server is None or not opcua_server.server_running:
        return jsonify({"status": "Server not started"}), 400

    name = request.form.get('name')
    dtype = request.form.get('dtype')
    min_val = float(request.form.get('min_val'))
    max_val = float(request.form.get('max_val'))
    gain = float(request.form.get('gain'))
    interval = float(request.form.get('interval'))

    try:
        opcua_server.add_tag(name, dtype, min_val)
        opcua_server.start_simulation(name, dtype, min_val, max_val, gain, interval)
        return jsonify({"status": f"Simulation tag {name} added and started"})
    except Exception as e:
        logging.error(f"Error adding simulation tag {name}: {e}")
        return jsonify({"status": f"Error adding simulation tag {name}"}), 500


@app.route('/remove_tag', methods=['POST'])
def remove_tag():
    name = request.form.get('name')
    opcua_server.remove_tag(name)
    if name in simulation_threads:
        del simulation_threads[name]
    return jsonify({"status": f"Tag {name} removed"})


@app.route('/remove_all_tags', methods=['POST'])
def remove_all_tags():
    if opcua_server is None or not opcua_server.server_running:
        return jsonify({"status": "Server not started"}), 400

    opcua_server.remove_all_tags()
    simulation_threads.clear()
    return jsonify({"status": "All tags removed"})


@app.route('/get_tags', methods=['GET'])
def get_tags():
    if opcua_server is None or not opcua_server.server_running:
        return jsonify({"tags": {}})

    tags = {name: {
        "type": tag.get_data_type_as_variant_type().name,
        "value": tag.get_value()
    }
        for name, tag in opcua_server.tags.items()}
    return jsonify({"tags": tags, "server_info": {"endpoint": opcua_server.endpoint, "uri": opcua_server.uri}})


@app.route('/get_server_info', methods=['GET'])
def get_server_info():
    if opcua_server and opcua_server.server_running:
        return jsonify({
            "endpoint": opcua_server.endpoint,
            "uri": opcua_server.uri
        })
    return jsonify({}), 404


if __name__ == "__main__":
    app.run(debug=True)
