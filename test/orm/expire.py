"""Attribute/instance expiration, deferral of attributes, etc."""

import testenv; testenv.configure_for_tests()
import gc
from testlib import sa, testing
from testlib.sa import Table, Column, Integer, String, ForeignKey, exc as sa_exc
from testlib.sa.orm import mapper, relation, create_session, attributes
from orm import _base, _fixtures


class ExpireTest(_fixtures.FixtureTest):

    @testing.resolve_artifact_names
    def test_expire(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user'),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(7)
        assert len(u.addresses) == 1
        u.name = 'foo'
        del u.addresses[0]
        sess.expire(u)

        assert 'name' not in u.__dict__

        def go():
            assert u.name == 'jack'
        self.assert_sql_count(testing.db, go, 1)
        assert 'name' in u.__dict__

        u.name = 'foo'
        sess.flush()
        # change the value in the DB
        users.update(users.c.id==7, values=dict(name='jack')).execute()
        sess.expire(u)
        # object isnt refreshed yet, using dict to bypass trigger
        assert u.__dict__.get('name') != 'jack'
        assert 'name' in attributes.instance_state(u).expired_attributes

        sess.query(User).all()
        # test that it refreshed
        assert u.__dict__['name'] == 'jack'
        assert 'name' not in attributes.instance_state(u).expired_attributes

        def go():
            assert u.name == 'jack'
        self.assert_sql_count(testing.db, go, 0)

    @testing.resolve_artifact_names
    def test_persistence_check(self):
        mapper(User, users)
        s = create_session()
        u = s.query(User).get(7)
        s.clear()

        self.assertRaisesMessage(sa.exc.InvalidRequestError, r"is not persistent within this Session", s.expire, u)

    @testing.resolve_artifact_names
    def test_get_refreshes(self):
        mapper(User, users)
        s = create_session(autocommit=False)
        u = s.query(User).get(10)
        s.expire_all()

        def go():
            u = s.query(User).get(10)  # get() refreshes
        self.assert_sql_count(testing.db, go, 1)
        def go():
            self.assertEquals(u.name, 'chuck')  # attributes unexpired
        self.assert_sql_count(testing.db, go, 0)
        def go():
            u = s.query(User).get(10)  # expire flag reset, so not expired
        self.assert_sql_count(testing.db, go, 0)

        s.expire_all()
        s.execute(users.delete().where(User.id==10))

        # object is gone, get() returns None, removes u from session
        assert u in s
        assert s.query(User).get(10) is None
        assert u not in s # and expunges

        # add it back
        s.add(u)
        # nope, raises ObjectDeletedError
        self.assertRaises(sa.orm.exc.ObjectDeletedError, getattr, u, 'name')

        # do a get()/remove u from session again
        assert s.query(User).get(10) is None
        assert u not in s
        
        s.rollback()

        assert u in s
        # but now its back, rollback has occured, the _remove_newly_deleted
        # is reverted
        self.assertEquals(u.name, 'chuck')
    
    @testing.resolve_artifact_names
    def test_lazyload_autoflushes(self):
        mapper(User, users, properties={
            'addresses':relation(Address, order_by=addresses.c.email_address)
        })
        mapper(Address, addresses)
        s = create_session(autoflush=True, autocommit=False)
        u = s.query(User).get(8)
        adlist = u.addresses
        self.assertEquals(adlist, [
            Address(email_address='ed@bettyboop.com'), 
            Address(email_address='ed@lala.com'),
            Address(email_address='ed@wood.com'), 
        ])
        a1 = u.addresses[2]
        a1.email_address = 'aaaaa'
        s.expire(u, ['addresses'])
        self.assertEquals(u.addresses, [
            Address(email_address='aaaaa'), 
            Address(email_address='ed@bettyboop.com'), 
            Address(email_address='ed@lala.com'),
        ])

    @testing.resolve_artifact_names
    def test_refresh_collection_exception(self):
        """test graceful failure for currently unsupported immediate refresh of a collection"""
        
        mapper(User, users, properties={
            'addresses':relation(Address, order_by=addresses.c.email_address)
        })
        mapper(Address, addresses)
        s = create_session(autoflush=True, autocommit=False)
        u = s.query(User).get(8)
        self.assertRaisesMessage(sa_exc.InvalidRequestError, "properties specified for refresh", s.refresh, u, ['addresses'])
        
        # in contrast to a regular query with no columns
        self.assertRaisesMessage(sa_exc.InvalidRequestError, "no columns with which to SELECT", s.query().all)
        
    @testing.resolve_artifact_names
    def test_refresh_cancels_expire(self):
        mapper(User, users)
        s = create_session()
        u = s.query(User).get(7)
        s.expire(u)
        s.refresh(u)

        def go():
            u = s.query(User).get(7)
            self.assertEquals(u.name, 'jack')
        self.assert_sql_count(testing.db, go, 0)

    @testing.resolve_artifact_names
    def test_expire_doesntload_on_set(self):
        mapper(User, users)

        sess = create_session()
        u = sess.query(User).get(7)

        sess.expire(u, attribute_names=['name'])
        def go():
            u.name = 'somenewname'
        self.assert_sql_count(testing.db, go, 0)
        sess.flush()
        sess.clear()
        assert sess.query(User).get(7).name == 'somenewname'

    @testing.resolve_artifact_names
    def test_no_session(self):
        mapper(User, users)
        sess = create_session()
        u = sess.query(User).get(7)

        sess.expire(u, attribute_names=['name'])
        sess.expunge(u)
        self.assertRaises(sa.exc.UnboundExecutionError, getattr, u, 'name')

    @testing.resolve_artifact_names
    def test_pending_raises(self):
        # this was the opposite in 0.4, but the reasoning there seemed off.
        # expiring a pending instance makes no sense, so should raise
        mapper(User, users)
        sess = create_session()
        u = User(id=15)
        sess.add(u)
        self.assertRaises(sa.exc.InvalidRequestError, sess.expire, u, ['name'])

    @testing.resolve_artifact_names
    def test_no_instance_key(self):
        # this tests an artificial condition such that
        # an instance is pending, but has expired attributes.  this
        # is actually part of a larger behavior when postfetch needs to
        # occur during a flush() on an instance that was just inserted
        mapper(User, users)
        sess = create_session()
        u = sess.query(User).get(7)

        sess.expire(u, attribute_names=['name'])
        sess.expunge(u)
        attributes.instance_state(u).key = None
        assert 'name' not in u.__dict__
        sess.add(u)
        assert u.name == 'jack'

    @testing.resolve_artifact_names
    def test_expire_preserves_changes(self):
        """test that the expire load operation doesn't revert post-expire changes"""

        mapper(Order, orders)
        sess = create_session()
        o = sess.query(Order).get(3)
        sess.expire(o)

        o.description = "order 3 modified"
        def go():
            assert o.isopen == 1
        self.assert_sql_count(testing.db, go, 1)
        assert o.description == 'order 3 modified'

        del o.description
        assert "description" not in o.__dict__
        sess.expire(o, ['isopen'])
        sess.query(Order).all()
        assert o.isopen == 1
        assert "description" not in o.__dict__

        assert o.description is None

        o.isopen=15
        sess.expire(o, ['isopen', 'description'])
        o.description = 'some new description'
        sess.query(Order).all()
        assert o.isopen == 1
        assert o.description == 'some new description'

        sess.expire(o, ['isopen', 'description'])
        sess.query(Order).all()
        del o.isopen
        def go():
            assert o.isopen is None
        self.assert_sql_count(testing.db, go, 0)

        o.isopen=14
        sess.expire(o)
        o.description = 'another new description'
        sess.query(Order).all()
        assert o.isopen == 1
        assert o.description == 'another new description'

    @testing.resolve_artifact_names
    def test_expire_committed(self):
        """test that the committed state of the attribute receives the most recent DB data"""
        mapper(Order, orders)

        sess = create_session()
        o = sess.query(Order).get(3)
        sess.expire(o)

        orders.update(id=3).execute(description='order 3 modified')
        assert o.isopen == 1
        assert attributes.instance_state(o).dict['description'] == 'order 3 modified'
        def go():
            sess.flush()
        self.assert_sql_count(testing.db, go, 0)

    @testing.resolve_artifact_names
    def test_expire_cascade(self):
        mapper(User, users, properties={
            'addresses':relation(Address, cascade="all, refresh-expire")
        })
        mapper(Address, addresses)
        s = create_session()
        u = s.query(User).get(8)
        assert u.addresses[0].email_address == 'ed@wood.com'

        u.addresses[0].email_address = 'someotheraddress'
        s.expire(u)
        u.name
        print attributes.instance_state(u).dict
        assert u.addresses[0].email_address == 'ed@wood.com'

    @testing.resolve_artifact_names
    def test_expired_lazy(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user'),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(7)

        sess.expire(u)
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__

        def go():
            assert u.addresses[0].email_address == 'jack@bean.com'
            assert u.name == 'jack'
        # two loads
        self.assert_sql_count(testing.db, go, 2)
        assert 'name' in u.__dict__
        assert 'addresses' in u.__dict__

    @testing.resolve_artifact_names
    def test_expired_eager(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(7)

        sess.expire(u)
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__

        def go():
            assert u.addresses[0].email_address == 'jack@bean.com'
            assert u.name == 'jack'
        # two loads, since relation() + scalar are
        # separate right now on per-attribute load
        self.assert_sql_count(testing.db, go, 2)
        assert 'name' in u.__dict__
        assert 'addresses' in u.__dict__

        sess.expire(u, ['name', 'addresses'])
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__

        def go():
            sess.query(User).filter_by(id=7).one()
            assert u.addresses[0].email_address == 'jack@bean.com'
            assert u.name == 'jack'
        # one load, since relation() + scalar are
        # together when eager load used with Query
        self.assert_sql_count(testing.db, go, 1)

    @testing.resolve_artifact_names
    def test_relation_changes_preserved(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)
        sess = create_session()
        u = sess.query(User).get(8)
        sess.expire(u, ['name', 'addresses'])
        u.addresses
        assert 'name' not in u.__dict__
        del u.addresses[1]
        u.name
        assert 'name' in u.__dict__
        assert len(u.addresses) == 2

    @testing.resolve_artifact_names
    def test_eagerload_props_dontload(self):
        # relations currently have to load separately from scalar instances.
        # the use case is: expire "addresses".  then access it.  lazy load
        # fires off to load "addresses", but needs foreign key or primary key
        # attributes in order to lazy load; hits those attributes, such as
        # below it hits "u.id".  "u.id" triggers full unexpire operation,
        # eagerloads addresses since lazy=False.  this is all wihtin lazy load
        # which fires unconditionally; so an unnecessary eagerload (or
        # lazyload) was issued.  would prefer not to complicate lazyloading to
        # "figure out" that the operation should be aborted right now.

        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)
        sess = create_session()
        u = sess.query(User).get(8)
        sess.expire(u)
        u.id
        assert 'addresses' not in u.__dict__
        u.addresses
        assert 'addresses' in u.__dict__

    @testing.resolve_artifact_names
    def test_expire_synonym(self):
        mapper(User, users, properties={
            'uname': sa.orm.synonym('name')
        })

        sess = create_session()
        u = sess.query(User).get(7)
        assert 'name' in u.__dict__
        assert u.uname == u.name

        sess.expire(u)
        assert 'name' not in u.__dict__

        users.update(users.c.id==7).execute(name='jack2')
        assert u.name == 'jack2'
        assert u.uname == 'jack2'
        assert 'name' in u.__dict__

        # this wont work unless we add API hooks through the attr. system to
        # provide "expire" behavior on a synonym
        #    sess.expire(u, ['uname'])
        #    users.update(users.c.id==7).execute(name='jack3')
        #    assert u.uname == 'jack3'

    @testing.resolve_artifact_names
    def test_partial_expire(self):
        mapper(Order, orders)

        sess = create_session()
        o = sess.query(Order).get(3)

        sess.expire(o, attribute_names=['description'])
        assert 'id' in o.__dict__
        assert 'description' not in o.__dict__
        assert attributes.instance_state(o).dict['isopen'] == 1

        orders.update(orders.c.id==3).execute(description='order 3 modified')

        def go():
            assert o.description == 'order 3 modified'
        self.assert_sql_count(testing.db, go, 1)
        assert attributes.instance_state(o).dict['description'] == 'order 3 modified'

        o.isopen = 5
        sess.expire(o, attribute_names=['description'])
        assert 'id' in o.__dict__
        assert 'description' not in o.__dict__
        assert o.__dict__['isopen'] == 5
        assert attributes.instance_state(o).committed_state['isopen'] == 1

        def go():
            assert o.description == 'order 3 modified'
        self.assert_sql_count(testing.db, go, 1)
        assert o.__dict__['isopen'] == 5
        assert attributes.instance_state(o).dict['description'] == 'order 3 modified'
        assert attributes.instance_state(o).committed_state['isopen'] == 1

        sess.flush()

        sess.expire(o, attribute_names=['id', 'isopen', 'description'])
        assert 'id' not in o.__dict__
        assert 'isopen' not in o.__dict__
        assert 'description' not in o.__dict__
        def go():
            assert o.description == 'order 3 modified'
            assert o.id == 3
            assert o.isopen == 5
        self.assert_sql_count(testing.db, go, 1)

    @testing.resolve_artifact_names
    def test_partial_expire_lazy(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user'),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(8)

        sess.expire(u, ['name', 'addresses'])
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__

        # hit the lazy loader.  just does the lazy load,
        # doesnt do the overall refresh
        def go():
            assert u.addresses[0].email_address=='ed@wood.com'
        self.assert_sql_count(testing.db, go, 1)

        assert 'name' not in u.__dict__

        # check that mods to expired lazy-load attributes
        # only do the lazy load
        sess.expire(u, ['name', 'addresses'])
        def go():
            u.addresses = [Address(id=10, email_address='foo@bar.com')]
        self.assert_sql_count(testing.db, go, 1)

        sess.flush()

        # flush has occurred, and addresses was modified,
        # so the addresses collection got committed and is
        # longer expired
        def go():
            assert u.addresses[0].email_address=='foo@bar.com'
            assert len(u.addresses) == 1
        self.assert_sql_count(testing.db, go, 0)

        # but the name attribute was never loaded and so
        # still loads
        def go():
            assert u.name == 'ed'
        self.assert_sql_count(testing.db, go, 1)

    @testing.resolve_artifact_names
    def test_partial_expire_eager(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(8)

        sess.expire(u, ['name', 'addresses'])
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__

        def go():
            assert u.addresses[0].email_address=='ed@wood.com'
        self.assert_sql_count(testing.db, go, 1)

        # check that mods to expired eager-load attributes
        # do the refresh
        sess.expire(u, ['name', 'addresses'])
        def go():
            u.addresses = [Address(id=10, email_address='foo@bar.com')]
        self.assert_sql_count(testing.db, go, 1)
        sess.flush()

        # this should ideally trigger the whole load
        # but currently it works like the lazy case
        def go():
            assert u.addresses[0].email_address=='foo@bar.com'
            assert len(u.addresses) == 1
        self.assert_sql_count(testing.db, go, 0)

        def go():
            assert u.name == 'ed'
        # scalar attributes have their own load
        self.assert_sql_count(testing.db, go, 1)
        # ideally, this was already loaded, but we arent
        # doing it that way right now
        #self.assert_sql_count(testing.db, go, 0)

    @testing.resolve_artifact_names
    def test_relations_load_on_query(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user'),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(8)
        assert 'name' in u.__dict__
        u.addresses
        assert 'addresses' in u.__dict__

        sess.expire(u, ['name', 'addresses'])
        assert 'name' not in u.__dict__
        assert 'addresses' not in u.__dict__
        (sess.query(User).options(sa.orm.eagerload('addresses')).
         filter_by(id=8).all())
        assert 'name' in u.__dict__
        assert 'addresses' in u.__dict__

    @testing.resolve_artifact_names
    def test_partial_expire_deferred(self):
        mapper(Order, orders, properties={
            'description': sa.orm.deferred(orders.c.description)
        })

        sess = create_session()
        o = sess.query(Order).get(3)
        sess.expire(o, ['description', 'isopen'])
        assert 'isopen' not in o.__dict__
        assert 'description' not in o.__dict__

        # test that expired attribute access refreshes
        # the deferred
        def go():
            assert o.isopen == 1
            assert o.description == 'order 3'
        self.assert_sql_count(testing.db, go, 1)

        sess.expire(o, ['description', 'isopen'])
        assert 'isopen' not in o.__dict__
        assert 'description' not in o.__dict__
        # test that the deferred attribute triggers the full
        # reload
        def go():
            assert o.description == 'order 3'
            assert o.isopen == 1
        self.assert_sql_count(testing.db, go, 1)

        sa.orm.clear_mappers()

        mapper(Order, orders)
        sess.clear()

        # same tests, using deferred at the options level
        o = sess.query(Order).options(sa.orm.defer('description')).get(3)

        assert 'description' not in o.__dict__

        # sanity check
        def go():
            assert o.description == 'order 3'
        self.assert_sql_count(testing.db, go, 1)

        assert 'description' in o.__dict__
        assert 'isopen' in o.__dict__
        sess.expire(o, ['description', 'isopen'])
        assert 'isopen' not in o.__dict__
        assert 'description' not in o.__dict__

        # test that expired attribute access refreshes
        # the deferred
        def go():
            assert o.isopen == 1
            assert o.description == 'order 3'
        self.assert_sql_count(testing.db, go, 1)
        sess.expire(o, ['description', 'isopen'])

        assert 'isopen' not in o.__dict__
        assert 'description' not in o.__dict__
        # test that the deferred attribute triggers the full
        # reload
        def go():
            assert o.description == 'order 3'
            assert o.isopen == 1
        self.assert_sql_count(testing.db, go, 1)

    @testing.resolve_artifact_names
    def test_eagerload_query_refreshes(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)

        sess = create_session()
        u = sess.query(User).get(8)
        assert len(u.addresses) == 3
        sess.expire(u)
        assert 'addresses' not in u.__dict__
        print "-------------------------------------------"
        sess.query(User).filter_by(id=8).all()
        assert 'addresses' in u.__dict__
        assert len(u.addresses) == 3

    @testing.resolve_artifact_names
    def test_expire_all(self):
        mapper(User, users, properties={
            'addresses':relation(Address, backref='user', lazy=False),
            })
        mapper(Address, addresses)

        sess = create_session()
        userlist = sess.query(User).order_by(User.id).all()
        assert self.static.user_address_result == userlist
        assert len(list(sess)) == 9
        sess.expire_all()
        gc.collect()
        assert len(list(sess)) == 4 # since addresses were gc'ed

        userlist = sess.query(User).order_by(User.id).all()
        u = userlist[1]
        self.assertEquals(self.static.user_address_result, userlist)
        assert len(list(sess)) == 9

class PolymorphicExpireTest(_base.MappedTest):
    run_inserts = 'once'
    run_deletes = None

    def define_tables(self, metadata):
        global people, engineers, Person, Engineer

        people = Table('people', metadata,
           Column('person_id', Integer, primary_key=True,
                  test_needs_autoincrement=True),
           Column('name', String(50)),
           Column('type', String(30)))

        engineers = Table('engineers', metadata,
           Column('person_id', Integer, ForeignKey('people.person_id'),
                  primary_key=True),
           Column('status', String(30)),
          )

    def setup_classes(self):
        class Person(_base.ComparableEntity):
            pass
        class Engineer(Person):
            pass

    @testing.resolve_artifact_names
    def insert_data(self):
        people.insert().execute(
            {'person_id':1, 'name':'person1', 'type':'person'},
            {'person_id':2, 'name':'engineer1', 'type':'engineer'},
            {'person_id':3, 'name':'engineer2', 'type':'engineer'},
        )
        engineers.insert().execute(
            {'person_id':2, 'status':'new engineer'},
            {'person_id':3, 'status':'old engineer'},
        )

    @testing.resolve_artifact_names
    def test_poly_deferred(self):
        mapper(Person, people, polymorphic_on=people.c.type, polymorphic_identity='person')
        mapper(Engineer, engineers, inherits=Person, polymorphic_identity='engineer')

        sess = create_session()
        [p1, e1, e2] = sess.query(Person).order_by(people.c.person_id).all()

        sess.expire(p1)
        sess.expire(e1, ['status'])
        sess.expire(e2)

        for p in [p1, e2]:
            assert 'name' not in p.__dict__

        assert 'name' in e1.__dict__
        assert 'status' not in e2.__dict__
        assert 'status' not in e1.__dict__

        e1.name = 'new engineer name'

        def go():
            sess.query(Person).all()
        self.assert_sql_count(testing.db, go, 1)

        for p in [p1, e1, e2]:
            assert 'name' in p.__dict__

        assert 'status' not in e2.__dict__
        assert 'status' not in e1.__dict__

        def go():
            assert e1.name == 'new engineer name'
            assert e2.name == 'engineer2'
            assert e1.status == 'new engineer'
            assert e2.status == 'old engineer'
        self.assert_sql_count(testing.db, go, 2)
        self.assertEquals(Engineer.name.get_history(e1), (['new engineer name'],(), ['engineer1']))


class RefreshTest(_fixtures.FixtureTest):

    @testing.resolve_artifact_names
    def test_refresh(self):
        mapper(User, users, properties={
            'addresses':relation(mapper(Address, addresses), backref='user')
        })
        s = create_session()
        u = s.query(User).get(7)
        u.name = 'foo'
        a = Address()
        assert sa.orm.object_session(a) is None
        u.addresses.append(a)
        assert a.email_address is None
        assert id(a) in [id(x) for x in u.addresses]

        s.refresh(u)

        # its refreshed, so not dirty
        assert u not in s.dirty

        # username is back to the DB
        assert u.name == 'jack'

        assert id(a) not in [id(x) for x in u.addresses]

        u.name = 'foo'
        u.addresses.append(a)
        # now its dirty
        assert u in s.dirty
        assert u.name == 'foo'
        assert id(a) in [id(x) for x in u.addresses]
        s.expire(u)

        # get the attribute, it refreshes
        print "OK------"
#        print u.__dict__
#        print u._state.callables
        assert u.name == 'jack'
        assert id(a) not in [id(x) for x in u.addresses]

    @testing.resolve_artifact_names
    def test_persistence_check(self):
        mapper(User, users)
        s = create_session()
        u = s.query(User).get(7)
        s.clear()
        self.assertRaisesMessage(sa.exc.InvalidRequestError, r"is not persistent within this Session", lambda: s.refresh(u))

    @testing.resolve_artifact_names
    def test_refresh_expired(self):
        mapper(User, users)
        s = create_session()
        u = s.query(User).get(7)
        s.expire(u)
        assert 'name' not in u.__dict__
        s.refresh(u)
        assert u.name == 'jack'

    @testing.resolve_artifact_names
    def test_refresh_with_lazy(self):
        """test that when a lazy loader is set as a trigger on an object's attribute
        (at the attribute level, not the class level), a refresh() operation doesnt
        fire the lazy loader or create any problems"""

        s = create_session()
        mapper(User, users, properties={'addresses':relation(mapper(Address, addresses))})
        q = s.query(User).options(sa.orm.lazyload('addresses'))
        u = q.filter(users.c.id==8).first()
        def go():
            s.refresh(u)
        self.assert_sql_count(testing.db, go, 1)

    @testing.resolve_artifact_names
    def test_refresh_with_eager(self):
        """test that a refresh/expire operation loads rows properly and sends correct "isnew" state to eager loaders"""

        mapper(User, users, properties={
            'addresses':relation(mapper(Address, addresses), lazy=False)
        })

        s = create_session()
        u = s.query(User).get(8)
        assert len(u.addresses) == 3
        s.refresh(u)
        assert len(u.addresses) == 3

        s = create_session()
        u = s.query(User).get(8)
        assert len(u.addresses) == 3
        s.expire(u)
        assert len(u.addresses) == 3

    @testing.fails_on('maxdb', 'FIXME: unknown')
    @testing.resolve_artifact_names
    def test_refresh2(self):
        """test a hang condition that was occuring on expire/refresh"""

        s = create_session()
        mapper(Address, addresses)

        mapper(User, users, properties = dict(addresses=relation(Address,cascade="all, delete-orphan",lazy=False)) )

        u = User()
        u.name='Justin'
        a = Address(id=10, email_address='lala')
        u.addresses.append(a)

        s.add(u)
        s.flush()
        s.clear()
        u = s.query(User).filter(User.name=='Justin').one()

        s.expire(u)
        assert u.name == 'Justin'

        s.refresh(u)

if __name__ == '__main__':
    testenv.main()
