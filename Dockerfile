FROM python:3.11-slim-bullseye

# Install minimal system dependencies
RUN apt-get update && apt-get install -y \
    python3-serial \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Create logs directory inside the container
RUN mkdir -p /app/logs && chmod 777 /app/logs

# Copy app files
COPY app/ .

# Install Python dependencies
RUN pip install --no-cache-dir flask==2.0.1 && \
    pip install --no-cache-dir pyserial==3.5 && \
    pip install --no-cache-dir requests==2.28.1 && \
    pip install --no-cache-dir websockets==12.0 && \
    pip install --no-cache-dir Werkzeug==2.0.3 && \
    pip install --no-cache-dir Jinja2==3.0.3 && \
    pip install --no-cache-dir MarkupSafe==2.0.1 && \
    pip install --no-cache-dir itsdangerous==2.0.1 && \
    pip install --no-cache-dir flask-cors==3.0.10 && \
    pip install --no-cache-dir waitress==2.1.2

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Flask on port 8666, WebSocket on port 8765
EXPOSE 8666/tcp
EXPOSE 8765/tcp

LABEL version="1.0.0"


LABEL permissions='\
{\
  "ExposedPorts": {\
    "8666/tcp": {},\
    "8765/tcp": {}\
  },\
  "HostConfig": {\
    "CpuPeriod": 100000,\
    "CpuQuota": 100000,\
    "Binds": [\
      "/usr/blueos/extensions/data-handler:/app/logs",\
      "/dev:/dev"\
    ],\
    "ExtraHosts": ["host.docker.internal:host-gateway"],\
    "PortBindings": {\
      "8666/tcp": [\
        {\
          "HostPort": "8666"\
        }\
      ],\
      "8765/tcp": [\
        {\
          "HostPort": "8765"\
        }\
      ]\
    },\
    "NetworkMode": "host",\
    "Privileged": true\
  }\
}'

LABEL authors='[\
  {\
    "name": "Tony White",\
    "email": "tony@bluerobotics.com"\
  }\
]'

LABEL company='{\
  "about": "",\
  "name": "Blue Robotics",\
  "email": "support@bluerobotics.com"\
}'

LABEL type="tool"
LABEL readme=''
LABEL links='{\
  "source": ""\
}'
LABEL requirements="core >= 1.1"
LABEL tags='[\
  "serial",\
  "mavlink",\
  "cockpit",\
  "data-lake",\
  "sensor"\
]'

CMD ["python", "main.py"]
