import testbase
import unittest

def suite():
    modules_to_test = (
        'dialect.access',
        'dialect.firebird',
        'dialect.informix',
        'dialect.mssql',
        'dialect.mysql',
        'dialect.oracle',
        'dialect.postgres',
        'dialect.sqlite',
        )
    alltests = unittest.TestSuite()
    for name in modules_to_test:
        mod = __import__(name)
        for token in name.split('.')[1:]:
            mod = getattr(mod, token)
        alltests.addTest(unittest.findTestCases(mod, suiteClass=None))
    return alltests



if __name__ == '__main__':
    testbase.main(suite())
