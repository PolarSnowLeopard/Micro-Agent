from flask import Flask, jsonify
from flask_restx import Api, Resource
from flask_cors import CORS
from main import func2, func5

app = Flask(__name__)
CORS(app)  # 启用跨域访问

# 配置Swagger文档
api = Api(
    app,
    version='1.0',
    title='Function API',
    description='API for accessing func2 and func5',
    doc='/swagger'
)

# 创建命名空间
ns = api.namespace('api', description='Function operations')

@ns.route('/func2')
class Func2Resource(Resource):
    def get(self):
        """执行func2函数"""
        try:
            # 由于原始函数只是打印，我们修改为返回结果
            result = "func2 executed successfully"
            func2()  # 实际执行函数
            return jsonify({"status": "success", "message": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

@ns.route('/func5')
class Func5Resource(Resource):
    def get(self):
        """执行func5函数"""
        try:
            # 由于原始函数只是打印，我们修改为返回结果
            result = "func5 executed successfully"
            func5()  # 实际执行函数
            return jsonify({"status": "success", "message": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)