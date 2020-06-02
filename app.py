#!/usr/bin/python3.6
import os
import time
import json
import paramiko
from flask import Flask, session, render_template, request
from flask_socketio import SocketIO, send, join_room
from flask_mysqldb import MySQL

app = Flask(__name__)
app.config['SECRET_KEY'] = 'NOqWF5R8sAhPO2paiAc3tJ0adCAqfVoJsw7PH8yLEXUGGLOXWWm3JUgnoII5xsNc'
app.config['MYSQL_HOST'] = '192.168.1.234'
app.config['MYSQL_USER'] = 'log_system'
app.config['MYSQL_PASSWORD'] = 'DxvwS1V2eqxDGwz2'
app.config['MYSQL_DB'] = 'log_system'
mysql = MySQL(app)
socketio = SocketIO(app)


def trace(stdout, room):
    for line in stdout:
        socketio.send(line, room=room)


@socketio.on('init')
def handle_init(data):
    if {'room', 'id', 'order'}.issubset(set(data.keys())):
        # 客户端要求执行的指令
        order = data['order']
        # 客户端指定的房间，如果没有指定房间，所有的消息都会堆积在一起
        room = data['room']
        session['room'] = room
        # 通过日志ID获取对应的服务器IP、端口、用户名、密码、日志类型、日志路径
        cursor = mysql.connection.cursor()
        cursor.execute(
            'SELECT `ip`, `port`, `username`, `password`, `log_type`, `log_path` FROM `server_info`, `server_log` '
            'WHERE `server_info`.`id` = `server_id` AND `server_log`.`id` = {}'.format(data['id'])
        )
        ip, port, username, password, log_type, log_path = cursor.fetchone()
        cursor.close()
        # 获取当天的日期
        today = time.strftime('%Y-%m-%d')
        # 获取客户端发送的日期，默认使用当天的日期
        date = data.get('date', today)
        # 如果日志类型为tomcat，就将日期附加在原来的日志路径上
        if log_type == 'tomcat':
            log_path += '.{}'.format(date)
        # 连接远程服务器
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, port, username, password)
        if order == 'trace':  # 实时追踪
            '''
            实时追踪是该项目的一个重难点，笔者在网上搜索许久，最终在stackoverflow中找到如下解决方案。
            一般来说，我们要实时追踪日志，只需执行“tail -f”命令即可，但这样会出现一个问题：客户端断开连接后“tail -f”会残留在服务器上。
            而执行“echo $$;exec tail -f”则可以预知tail命令的PID，在客户端断开连接前结束该进程。
            socketio有一点非常好，那就是支持多线程，执行“tail -f”命令后，我们需要不断地从stdout中获取结果，这是个死循环，
            如果放在前台执行，会阻塞其他消息，此时如果用到socketio的start_background_task，就可以解决此问题。
            '''
            _, stdout, _ = client.exec_command('echo $$;exec tail -f {}'.format(log_path))
            session['pid'] = int(stdout.readline())
            socketio.start_background_task(target=trace, stdout=stdout, room=room)
            session['client'] = client
        else:
            cmd = None
            if order == 'lastline':  # 查看最后N行
                if data.get('line_number'):
                    cmd = 'tail -n {} {}'.format(data['line_number'], log_path)
            elif order == 'keyword':  # 根据关键字查看
                '''
                spring日志当天和历史日志的路径不同。
                当天的日志直接存放在logs目录下，历史日志存放在logs/daily目录下，并且经过gzip压缩，文件名也有所变化。
                通过zcat命令可以在不解压的情况下查看日志，在通过grep命令将包含关键字的上下文返回客户端。
                '''
                if {'show_way', 'line_number', 'keyword'}.issubset(set(data.keys())):
                    show_way, line_number, keyword = data['show_way'], data['line_number'], data['keyword']
                    if log_type == 'spring' and date != today:
                        cmd = 'zcat {}.{}.gz | grep -{} {} "{}"'.format(
                            '/'.join((os.path.dirname(log_path), 'daily', os.path.basename(log_path))),
                            date,
                            show_way,
                            line_number,
                            keyword
                        )
                    else:
                        cmd = 'grep -{} {} "{}" {}'.format(show_way, line_number, keyword, log_path)
            elif order == 'time_quantm':  # 根据时间段查看
                if {'start_time', 'end_time'}.issubset(set(data.keys())):
                    start_time, end_time = data['start_time'], data['end_time']
                    if log_type == 'spring' and date != today:
                        cmd = """zcat {}.{}.gz | awk '{{split($2,a,/,/)}}a[1]>"{}"&&a[1]<"{}"'""".format(
                            '/'.join((os.path.dirname(log_path), 'daily', os.path.basename(log_path))),
                            date,
                            start_time,
                            end_time
                        )
                    else:
                        cmd = """awk '{{split($2,a,/,/)}}a[1]>"{}"&&a[1]<"{}"' {}""".format(
                            start_time,
                            end_time,
                            log_path
                        )
            if cmd:
                _, stdout, _ = client.exec_command(cmd)
                # 将日志发送到指定的客户端房间
                for line in stdout.xreadlines():
                    send(line, room=room)
            client.close()


@socketio.on('disconnect')
def handle_disconnect():
    print('disconnect!')
    # 适用于客户端没有主动关闭SSH连接的情况
    try:
        session['client'].exec_command('kill {}'.format(session['pid']))
        session['client'].close()
    except KeyError:
        pass
    except AttributeError:
        pass


@socketio.on('connect')
def handle_connect():
    print('connect!')


@socketio.on('message')
def handle_message(message):
    # 客户端主动发送指令要求关闭SSH连接
    if message == 'close':
        try:
            session['client'].exec_command('kill {}'.format(session['pid']))
            session['client'].close()
        except KeyError:
            pass
        except AttributeError:
            pass


@socketio.on('join')
def handle_join(room):
    # 将客户端加入指定房间
    join_room(room)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/get_distinct/<table>/<field>/')
def get_distinct(table, field):
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT DISTINCT `{}` FROM `{}`'.format(field, table))
    return json.dumps({'{}_list'.format(field): [row[0] for row in cursor.fetchall()]})


@app.route('/query/<table>/')
def query(table):
    sql = 'SELECT * FROM {} WHERE 1=1'.format(table)
    if table == 'server_log':
        sql = 'SELECT `server_log`.`id`, `environment`, `project`, `comment` FROM `server_info`, `server_log` ' \
              'WHERE `server_info`.`id` = `server_id`'
        if request.args.get('environment') and request.args['environment'] != '全部':
            sql += ' AND `environment` = "{}"'.format(request.args['environment'])
        if request.args.get('project') and request.args['project'] != '全部':
            sql += ' AND `project` = "{}"'.format(request.args['project'])
        if request.args.get('comment'):
            sql += ' AND `comment` LIKE "%{}%"'.format(request.args['comment'])
    if request.args.get('offset') and request.args.get('limit'):
        cursor = mysql.connection.cursor()
        offset = int(request.args['offset'])
        limit = int(request.args['limit'])
        if request.args.get('sort') and request.args.get('order'):
            sql += ' ORDER BY `{}` {}'.format(request.args['sort'], request.args['order'])
        cursor.execute(sql)
        row_list = cursor.fetchall()
        total = len(row_list)
        rows = [
            {name: value for name, value in zip([desc[0] for desc in cursor.description], row)}
            for row in row_list[offset:offset + limit]
        ]
        return json.dumps({'total': total, 'rows': rows})
    else:
        return 'ERROR'
        

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0')
