import testenv; testenv.configure_for_tests()
from sqlalchemy import *
from sqlalchemy import util
from sqlalchemy.orm import *
from testlib import *
from testlib import fixtures

class ABCTest(ORMTest):
    def define_tables(self, metadata):
        global a, b, c
        a = Table('a', metadata,
            Column('id', Integer, primary_key=True),
            Column('adata', String(30)),
            Column('type', String(30)),
            )
        b = Table('b', metadata,
            Column('id', Integer, ForeignKey('a.id'), primary_key=True),
            Column('bdata', String(30)))
        c = Table('c', metadata,
            Column('id', Integer, ForeignKey('b.id'), primary_key=True),
            Column('cdata', String(30)))

    def make_test(fetchtype):
        def test_roundtrip(self):
            class A(fixtures.Base):pass
            class B(A):pass
            class C(B):pass

            if fetchtype == 'union':
                abc = a.outerjoin(b).outerjoin(c)
                bc = a.join(b).outerjoin(c)
            else:
                abc = bc = None

            mapper(A, a, with_polymorphic=('*', abc), polymorphic_on=a.c.type, polymorphic_identity='a')
            mapper(B, b, with_polymorphic=('*', bc), inherits=A, polymorphic_identity='b')
            mapper(C, c, inherits=B, polymorphic_identity='c')

            a1 = A(adata='a1')
            b1 = B(bdata='b1', adata='b1')
            b2 = B(bdata='b2', adata='b2')
            b3 = B(bdata='b3', adata='b3')
            c1 = C(cdata='c1', bdata='c1', adata='c1')
            c2 = C(cdata='c2', bdata='c2', adata='c2')
            c3 = C(cdata='c2', bdata='c2', adata='c2')

            sess = create_session()
            for x in (a1, b1, b2, b3, c1, c2, c3):
                sess.save(x)
            sess.flush()
            sess.clear()

            #for obj in sess.query(A).all():
            #    print obj
            assert [
                A(adata='a1'),
                B(bdata='b1', adata='b1'),
                B(bdata='b2', adata='b2'),
                B(bdata='b3', adata='b3'),
                C(cdata='c1', bdata='c1', adata='c1'),
                C(cdata='c2', bdata='c2', adata='c2'),
                C(cdata='c2', bdata='c2', adata='c2'),
            ] == sess.query(A).all()

            assert [
                B(bdata='b1', adata='b1'),
                B(bdata='b2', adata='b2'),
                B(bdata='b3', adata='b3'),
                C(cdata='c1', bdata='c1', adata='c1'),
                C(cdata='c2', bdata='c2', adata='c2'),
                C(cdata='c2', bdata='c2', adata='c2'),
            ] == sess.query(B).all()

            assert [
                C(cdata='c1', bdata='c1', adata='c1'),
                C(cdata='c2', bdata='c2', adata='c2'),
                C(cdata='c2', bdata='c2', adata='c2'),
            ] == sess.query(C).all()

        test_roundtrip = _function_named(
            test_roundtrip, 'test_%s' % fetchtype)
        return test_roundtrip

    test_union = make_test('union')
    test_none = make_test('none')


if __name__ == '__main__':
    testenv.main()
