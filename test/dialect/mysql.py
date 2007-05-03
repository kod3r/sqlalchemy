from testbase import PersistTest, AssertMixin
import testbase
from sqlalchemy import *
from sqlalchemy.databases import mysql
import sys, StringIO

db = testbase.db

class TypesTest(AssertMixin):
    "Test MySQL column types"

    @testbase.supported('mysql')
    def test_numeric(self):
        "Exercise type specification and options for numeric types."
        
        columns = [
            # column type, args, kwargs, expected ddl
            # e.g. Column(Integer(10, unsigned=True)) == 'INTEGER(10) UNSIGNED'
            (mysql.MSNumeric, [], {},
             'NUMERIC(10, 2)'),
            (mysql.MSNumeric, [None], {},
             'NUMERIC'),
            (mysql.MSNumeric, [12], {},
             'NUMERIC(12, 2)'),
            (mysql.MSNumeric, [12, 4], {'unsigned':True},
             'NUMERIC(12, 4) UNSIGNED'),
            (mysql.MSNumeric, [12, 4], {'zerofill':True},
             'NUMERIC(12, 4) ZEROFILL'),
            (mysql.MSNumeric, [12, 4], {'zerofill':True, 'unsigned':True},
             'NUMERIC(12, 4) UNSIGNED ZEROFILL'),

            (mysql.MSDecimal, [], {},
             'DECIMAL(10, 2)'),
            (mysql.MSDecimal, [None], {},
             'DECIMAL'),
            (mysql.MSDecimal, [12], {},
             'DECIMAL(12, 2)'),
            (mysql.MSDecimal, [12, None], {},
             'DECIMAL(12)'),
            (mysql.MSDecimal, [12, 4], {'unsigned':True},
             'DECIMAL(12, 4) UNSIGNED'),
            (mysql.MSDecimal, [12, 4], {'zerofill':True},
             'DECIMAL(12, 4) ZEROFILL'),
            (mysql.MSDecimal, [12, 4], {'zerofill':True, 'unsigned':True},
             'DECIMAL(12, 4) UNSIGNED ZEROFILL'),

            (mysql.MSDouble, [None, None], {},
             'DOUBLE'),
            (mysql.MSDouble, [12], {},
             'DOUBLE(12, 2)'),
            (mysql.MSDouble, [12, 4], {'unsigned':True},
             'DOUBLE(12, 4) UNSIGNED'),
            (mysql.MSDouble, [12, 4], {'zerofill':True},
             'DOUBLE(12, 4) ZEROFILL'),
            (mysql.MSDouble, [12, 4], {'zerofill':True, 'unsigned':True},
             'DOUBLE(12, 4) UNSIGNED ZEROFILL'),

            (mysql.MSFloat, [], {},
             'FLOAT(10)'),
            (mysql.MSFloat, [None], {},
             'FLOAT'),
            (mysql.MSFloat, [12], {},
             'FLOAT(12)'),
            (mysql.MSFloat, [12, 4], {},
             'FLOAT(12, 4)'),
            (mysql.MSFloat, [12, 4], {'unsigned':True},
             'FLOAT(12, 4) UNSIGNED'),
            (mysql.MSFloat, [12, 4], {'zerofill':True},
             'FLOAT(12, 4) ZEROFILL'),
            (mysql.MSFloat, [12, 4], {'zerofill':True, 'unsigned':True},
             'FLOAT(12, 4) UNSIGNED ZEROFILL'),

            (mysql.MSInteger, [], {},
             'INTEGER'),
            (mysql.MSInteger, [4], {},
             'INTEGER(4)'),
            (mysql.MSInteger, [4], {'unsigned':True},
             'INTEGER(4) UNSIGNED'),
            (mysql.MSInteger, [4], {'zerofill':True},
             'INTEGER(4) ZEROFILL'),
            (mysql.MSInteger, [4], {'zerofill':True, 'unsigned':True},
             'INTEGER(4) UNSIGNED ZEROFILL'),

            (mysql.MSBigInteger, [], {},
             'BIGINT'),
            (mysql.MSBigInteger, [4], {},
             'BIGINT(4)'),
            (mysql.MSBigInteger, [4], {'unsigned':True},
             'BIGINT(4) UNSIGNED'),
            (mysql.MSBigInteger, [4], {'zerofill':True},
             'BIGINT(4) ZEROFILL'),
            (mysql.MSBigInteger, [4], {'zerofill':True, 'unsigned':True},
             'BIGINT(4) UNSIGNED ZEROFILL'),

            (mysql.MSSmallInteger, [], {},
             'SMALLINT'),
            (mysql.MSSmallInteger, [4], {},
             'SMALLINT(4)'),
            (mysql.MSSmallInteger, [4], {'unsigned':True},
             'SMALLINT(4) UNSIGNED'),
            (mysql.MSSmallInteger, [4], {'zerofill':True},
             'SMALLINT(4) ZEROFILL'),
            (mysql.MSSmallInteger, [4], {'zerofill':True, 'unsigned':True},
             'SMALLINT(4) UNSIGNED ZEROFILL'),
           ]

        table_args = ['test_mysql_numeric', db]
        for index, spec in enumerate(columns):
            type_, args, kw, res = spec
            table_args.append(Column('c%s' % index, type_(*args, **kw)))

        numeric_table = Table(*table_args)
        gen = db.dialect.schemagenerator(db, None, None)
        
        for col in numeric_table.c:
            index = int(col.name[1:])
            self.assertEquals(gen.get_column_specification(col),
                              "%s %s" % (col.name, columns[index][3]))

        try:
            numeric_table.create(checkfirst=True)
            assert True
        except:
            raise
        numeric_table.drop()
    
    @testbase.supported('mysql')
    def test_charset(self):
        """Exercise CHARACTER SET and COLLATE-related options on string-type
        columns."""

        columns = [
            (mysql.MSChar, [1], {},
             'CHAR(1)'),
            (mysql.MSChar, [1], {'binary':True},
             'CHAR(1) BINARY'),
            (mysql.MSChar, [1], {'ascii':True},
             'CHAR(1) ASCII'),
            (mysql.MSChar, [1], {'unicode':True},
             'CHAR(1) UNICODE'),
            (mysql.MSChar, [1], {'ascii':True, 'binary':True},
             'CHAR(1) ASCII BINARY'),
            (mysql.MSChar, [1], {'unicode':True, 'binary':True},
             'CHAR(1) UNICODE BINARY'),
            (mysql.MSChar, [1], {'charset':'utf8'},
             'CHAR(1) CHARACTER SET utf8'),
            (mysql.MSChar, [1], {'charset':'utf8', 'binary':True},
             'CHAR(1) CHARACTER SET utf8 BINARY'),
            (mysql.MSChar, [1], {'charset':'utf8', 'unicode':True},
             'CHAR(1) CHARACTER SET utf8'),
            (mysql.MSChar, [1], {'charset':'utf8', 'ascii':True},
             'CHAR(1) CHARACTER SET utf8'),
            (mysql.MSChar, [1], {'collation': 'utf8_bin'},
             'CHAR(1) COLLATE utf8_bin'),
            (mysql.MSChar, [1], {'charset': 'utf8', 'collation': 'utf8_bin'},
             'CHAR(1) CHARACTER SET utf8 COLLATE utf8_bin'),
            (mysql.MSChar, [1], {'charset': 'utf8', 'binary': True},
             'CHAR(1) CHARACTER SET utf8 BINARY'),
            (mysql.MSChar, [1], {'charset': 'utf8', 'collation': 'utf8_bin',
                              'binary': True},
             'CHAR(1) CHARACTER SET utf8 COLLATE utf8_bin'),
            (mysql.MSChar, [1], {'national':True},
             'NATIONAL CHAR(1)'),
            (mysql.MSChar, [1], {'national':True, 'charset':'utf8'},
             'NATIONAL CHAR(1)'),
            (mysql.MSChar, [1], {'national':True, 'charset':'utf8', 'binary':True},
             'NATIONAL CHAR(1) BINARY'),
            (mysql.MSChar, [1], {'national':True, 'binary':True, 'unicode':True},
             'NATIONAL CHAR(1) BINARY'),
            (mysql.MSChar, [1], {'national':True, 'collation':'utf8_bin'},
             'NATIONAL CHAR(1) COLLATE utf8_bin'),

            (mysql.MSString, [1], {'charset':'utf8', 'collation':'utf8_bin'},
             'VARCHAR(1) CHARACTER SET utf8 COLLATE utf8_bin'),
            (mysql.MSString, [1], {'national':True, 'collation':'utf8_bin'},
             'NATIONAL VARCHAR(1) COLLATE utf8_bin'),

            (mysql.MSTinyText, [], {'charset':'utf8', 'collation':'utf8_bin'},
             'TINYTEXT CHARACTER SET utf8 COLLATE utf8_bin'),

            (mysql.MSMediumText, [], {'charset':'utf8', 'binary':True},
             'MEDIUMTEXT CHARACTER SET utf8 BINARY'), 

            (mysql.MSLongText, [], {'ascii':True},
             'LONGTEXT ASCII'),

            (mysql.MSEnum, ["'foo'", "'bar'"], {'unicode':True},
             '''ENUM('foo','bar') UNICODE''')
           ]

        table_args = ['test_mysql_charset', db]
        for index, spec in enumerate(columns):
            type_, args, kw, res = spec
            table_args.append(Column('c%s' % index, type_(*args, **kw)))

        charset_table = Table(*table_args)
        gen = db.dialect.schemagenerator(db, None, None)
        
        for col in charset_table.c:
            index = int(col.name[1:])
            self.assertEquals(gen.get_column_specification(col),
                              "%s %s" % (col.name, columns[index][3]))

        try:
            charset_table.create(checkfirst=True)
            assert True
        except:
            raise
        charset_table.drop()

    @testbase.supported('mysql')
    def test_enum(self):
        "Exercise the ENUM type"

        enum_table = Table('mysql_enum', db,
            Column('e1', mysql.MSEnum('"a"', "'b'")),
            Column('e2', mysql.MSEnum('"a"', "'b'"), nullable=False),
            Column('e3', mysql.MSEnum('"a"', "'b'", strict=True)),
            Column('e4', mysql.MSEnum('"a"', "'b'", strict=True), nullable=False))
        spec = lambda c: db.dialect.schemagenerator(db, None, None).get_column_specification(c)

        self.assertEqual(spec(enum_table.c.e1), """e1 ENUM("a",'b')""")
        self.assertEqual(spec(enum_table.c.e2), """e2 ENUM("a",'b') NOT NULL""")
        self.assertEqual(spec(enum_table.c.e3), """e3 ENUM("a",'b')""")
        self.assertEqual(spec(enum_table.c.e4), """e4 ENUM("a",'b') NOT NULL""")
        enum_table.drop()
        enum_table.create()

        try:
            enum_table.insert().execute(e1=None, e2=None, e3=None, e4=None)
            self.assert_(False)
        except exceptions.SQLError:
            self.assert_(True)

        try:
            enum_table.insert().execute(e1='c', e2='c', e3='c', e4='c')
            self.assert_(False)
        except exceptions.InvalidRequestError:
            self.assert_(True)

        enum_table.insert().execute()
        enum_table.insert().execute(e1='a', e2='a', e3='a', e4='a')
        enum_table.insert().execute(e1='b', e2='b', e3='b', e4='b')

        # Insert out of range enums, push stderr aside to avoid expected
        # warnings cluttering test output
        try:
            aside = sys.stderr
            sys.stderr = StringIO.StringIO()

            con = db.connect()
            self.assert_(not con.connection.show_warnings())
            con.execute(insert(enum_table, {'e1':'c', 'e2':'c',
                                            'e3':'a', 'e4':'a'}))
            self.assert_(con.connection.show_warnings())
        finally:
            sys.stderr = aside

        res = enum_table.select().execute().fetchall()

        # This is known to fail with MySQLDB versions < 1.2.2
        self.assertEqual(res, [(None, 'a', None, 'a'),
                               ('a', 'a', 'a', 'a'),
                               ('b', 'b', 'b', 'b'),
                               ('', '', 'a', 'a')])
        enum_table.drop()

if __name__ == "__main__":
    testbase.main()