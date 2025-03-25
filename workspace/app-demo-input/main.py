def func1():
    print("func1")

def func2():
    print("func2")

def func3():
    func1()
    print("func3")

def func4():
    func3()
    print("func4")

def func5():
    func4()
    print("func5")

def func6():
    func5()
    print("func6")

def main():
    func1()
    func2()
    func3()
    func6()
    print("Hello, World!")

if __name__ == "__main__":
    main()
