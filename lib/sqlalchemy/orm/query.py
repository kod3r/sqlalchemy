# orm/query.py
# Copyright (C) 2005, 2006, 2007, 2008 Michael Bayer mike_mp@zzzcomputing.com
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""The Query class and support.

Defines the [sqlalchemy.orm.query#Query] class, the central construct used by
the ORM to construct database queries.

The ``Query`` class should not be confused with the
[sqlalchemy.sql.expression#Select] class, which defines database SELECT
operations at the SQL (non-ORM) level.  ``Query`` differs from ``Select`` in
that it returns ORM-mapped objects and interacts with an ORM session, whereas
the ``Select`` construct interacts directly with the database to return
iterable result sets.

"""

from itertools import chain

from sqlalchemy import sql, util, log, schema
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import exc as orm_exc
from sqlalchemy.sql import util as sql_util
from sqlalchemy.sql import expression, visitors, operators
from sqlalchemy.orm import attributes, interfaces, mapper, object_mapper, evaluator
from sqlalchemy.orm.util import _state_mapper, _is_mapped_class, \
     _is_aliased_class, _entity_descriptor, _entity_info, _class_to_mapper, \
     _orm_columns, AliasedClass, _orm_selectable, join as orm_join, ORMAdapter

__all__ = ['Query', 'QueryContext', 'aliased']


aliased = AliasedClass

def _generative(*assertions):
    """mark a method as generative."""

    def decorate(fn):
        argspec = util.format_argspec_plus(fn)
        run_assertions = assertions
        code = "\n".join([
            "def %s%s:",
            "    %r",
            "    self = self._clone()",
            "    for a in run_assertions:",
            "        a(self, %r)",
            "    fn%s",
            "    return self"
        ]) % (fn.__name__, argspec['args'], fn.__doc__, fn.__name__, argspec['apply_pos'])
        env = locals().copy()
        exec code in env
        return env[fn.__name__]
    return decorate

class Query(object):
    """Encapsulates the object-fetching operations provided by Mappers."""

    def __init__(self, entities, session=None, entity_name=None):
        self.session = session

        self._with_options = []
        self._lockmode = None
        self._order_by = False
        self._group_by = False
        self._distinct = False
        self._offset = None
        self._limit = None
        self._statement = None
        self._params = {}
        self._yield_per = None
        self._criterion = None
        self._correlate = util.Set()
        self._joinpoint = None
        self._with_labels = False
        self.__joinable_tables = None
        self._having = None
        self._populate_existing = False
        self._version_check = False
        self._autoflush = True
        self._attributes = {}
        self._current_path = ()
        self._only_load_props = None
        self._refresh_state = None
        self._from_obj = None
        self._entities = []
        self._polymorphic_adapters = {}
        self._filter_aliases = None
        self._from_obj_alias = None
        self.__currenttables = util.Set()

        for ent in util.to_list(entities):
            _QueryEntity(self, ent, entity_name=entity_name)

        self.__setup_aliasizers(self._entities)

    def __setup_aliasizers(self, entities):
        d = {}
        for ent in entities:
            for entity in ent.entities:
                if entity not in d:
                    mapper, selectable, is_aliased_class = _entity_info(entity, ent.entity_name)
                    if not is_aliased_class and mapper.with_polymorphic:
                        with_polymorphic = mapper._with_polymorphic_mappers
                        self.__mapper_loads_polymorphically_with(mapper, sql_util.ColumnAdapter(selectable, mapper._equivalent_columns))
                        adapter = None
                    elif is_aliased_class:
                        adapter = sql_util.ColumnAdapter(selectable, mapper._equivalent_columns)
                        with_polymorphic = None
                    else:
                        with_polymorphic = adapter = None

                    d[entity] = (mapper, adapter, selectable, is_aliased_class, with_polymorphic)
                ent.setup_entity(entity, *d[entity])

    def __mapper_loads_polymorphically_with(self, mapper, adapter):
        for m2 in mapper._with_polymorphic_mappers:
            for m in m2.iterate_to_root():
                self._polymorphic_adapters[m.mapped_table] = self._polymorphic_adapters[m.local_table] = adapter

    def __set_select_from(self, from_obj):
        if isinstance(from_obj, expression._SelectBaseMixin):
            # alias SELECTs and unions
            from_obj = from_obj.alias()

        self._from_obj = from_obj
        equivs = self.__all_equivs()

        if isinstance(from_obj, expression.Alias):
            # dont alias a regular join (since its not an alias itself)
            self._from_obj_alias = sql_util.ColumnAdapter(self._from_obj, equivs)

    def _get_polymorphic_adapter(self, entity, selectable):
        self.__mapper_loads_polymorphically_with(entity.mapper, sql_util.ColumnAdapter(selectable, entity.mapper._equivalent_columns))

    def _reset_polymorphic_adapter(self, mapper):
        for m2 in mapper._with_polymorphic_mappers:
            for m in m2.iterate_to_root():
                self._polymorphic_adapters.pop(m.mapped_table, None)
                self._polymorphic_adapters.pop(m.local_table, None)

    def __reset_joinpoint(self):
        self._joinpoint = None
        self._filter_aliases = None

    def __adapt_polymorphic_element(self, element):
        if isinstance(element, expression.FromClause):
            search = element
        elif hasattr(element, 'table'):
            search = element.table
        else:
            search = None

        if search:
            alias = self._polymorphic_adapters.get(search, None)
            if alias:
                return alias.adapt_clause(element)
    
    def __replace_element(self, adapters):
        def replace(elem):
            if '_halt_adapt' in elem._annotations:
                return elem

            for adapter in adapters:
                e = adapter(elem)
                if e:
                    return e
        return replace
    
    def __replace_orm_element(self, adapters):
        def replace(elem):
            if '_halt_adapt' in elem._annotations:
                return elem

            if "_orm_adapt" in elem._annotations or "parententity" in elem._annotations:
                for adapter in adapters:
                    e = adapter(elem)
                    if e:
                        return e
        return replace

    def _adapt_all_clauses(self):
        self._disable_orm_filtering = True
    _adapt_all_clauses = _generative()(_adapt_all_clauses)
    
    def _adapt_clause(self, clause, as_filter, orm_only):
        adapters = []    
        if as_filter and self._filter_aliases:
            adapters.append(self._filter_aliases.replace)

        if self._polymorphic_adapters:
            adapters.append(self.__adapt_polymorphic_element)

        if self._from_obj_alias:
            adapters.append(self._from_obj_alias.replace)

        if not adapters:
            return clause
            
        if getattr(self, '_disable_orm_filtering', not orm_only):
            return visitors.replacement_traverse(clause, {'column_collections':False}, self.__replace_element(adapters))
        else:
            return visitors.replacement_traverse(clause, {'column_collections':False}, self.__replace_orm_element(adapters))
        
    def _entity_zero(self):
        return self._entities[0]

    def _mapper_zero(self):
        return self._entity_zero().entity_zero

    def _extension_zero(self):
        ent = self._entity_zero()
        return getattr(ent, 'extension', ent.mapper.extension)

    def _mapper_entities(self):
        for ent in self._entities:
            if hasattr(ent, 'primary_entity'):
                yield ent
    _mapper_entities = property(_mapper_entities)

    def _joinpoint_zero(self):
        return self._joinpoint or self._entity_zero().entity_zero

    def _mapper_zero_or_none(self):
        if not getattr(self._entities[0], 'primary_entity', False):
            return None
        return self._entities[0].mapper

    def _only_mapper_zero(self):
        if len(self._entities) > 1:
            raise sa_exc.InvalidRequestError("This operation requires a Query against a single mapper.")
        return self._mapper_zero()

    def _only_entity_zero(self):
        if len(self._entities) > 1:
            raise sa_exc.InvalidRequestError("This operation requires a Query against a single mapper.")
        return self._entity_zero()

    def _generate_mapper_zero(self):
        if not getattr(self._entities[0], 'primary_entity', False):
            raise sa_exc.InvalidRequestError("No primary mapper set up for this Query.")
        entity = self._entities[0]._clone()
        self._entities = [entity] + self._entities[1:]
        return entity

    def __mapper_zero_from_obj(self):
        if self._from_obj:
            return self._from_obj
        else:
            return self._entity_zero().selectable

    def __all_equivs(self):
        equivs = {}
        for ent in self._mapper_entities:
            equivs.update(ent.mapper._equivalent_columns)
        return equivs

    def __no_criterion_condition(self, meth):
        if self._criterion or self._statement or self._from_obj or self._limit is not None or self._offset is not None or self._group_by or self._order_by:
            raise sa_exc.InvalidRequestError("Query.%s() being called on a Query with existing criterion. " % meth)

        self._statement = self._criterion = self._from_obj = None
        self._order_by = self._group_by = self._distinct = False
        self.__joined_tables = {}

    def __no_from_condition(self, meth):
        if self._from_obj:
            raise sa_exc.InvalidRequestError("Query.%s() being called on a Query which already has a FROM clause established.  This usage is deprecated." % meth)

    def __no_statement_condition(self, meth):
        if self._statement:
            raise sa_exc.InvalidRequestError(
                ("Query.%s() being called on a Query with an existing full "
                 "statement - can't apply criterion.") % meth)

    def __no_limit_offset(self, meth):
        if self._limit is not None or self._offset is not None:
            # TODO: do we want from_self() to be implicit here ?  i vote explicit for the time being
            raise sa_exc.InvalidRequestError("Query.%s() being called on a Query which already has LIMIT or OFFSET applied. "
            "To modify the row-limited results of a Query, call from_self() first.  Otherwise, call %s() before limit() or offset() are applied." % (meth, meth)
            )

    def __no_criterion(self):
        """generate a Query with no criterion, warn if criterion was present"""
    __no_criterion = _generative(__no_criterion_condition)(__no_criterion)

    def __get_options(self, populate_existing=None, version_check=None, only_load_props=None, refresh_state=None):
        if populate_existing:
            self._populate_existing = populate_existing
        if version_check:
            self._version_check = version_check
        if refresh_state:
            self._refresh_state = refresh_state
        if only_load_props:
            self._only_load_props = util.Set(only_load_props)
        return self

    def _clone(self):
        q = Query.__new__(Query)
        q.__dict__ = self.__dict__.copy()
        return q

    def statement(self):
        """return the full SELECT statement represented by this Query."""
        return self._compile_context(labels=self._with_labels).statement._annotate({'_halt_adapt': True})
    statement = property(statement)

    def subquery(self):
        """return the full SELECT statement represented by this Query, embedded within an Alias."""
        
        return self.statement.alias()
        
    def with_labels(self):
        """Apply column labels to the return value of Query.statement.
        
        Indicates that this Query's `statement` accessor should return a SELECT statement
        that applies labels to all columns in the form <tablename>_<columnname>; this
        is commonly used to disambiguate columns from multiple tables which have the
        same name.
        
        When the `Query` actually issues SQL to load rows, it always uses 
        column labeling.
        
        """
        self._with_labels = True
    with_labels = _generative()(with_labels)
    
    
    def whereclause(self):
        """return the WHERE criterion for this Query."""
        return self._criterion
    whereclause = property(whereclause)

    def _with_current_path(self, path):
        """indicate that this query applies to objects loaded within a certain path.

        Used by deferred loaders (see strategies.py) which transfer query
        options from an originating query to a newly generated query intended
        for the deferred load.

        """
        self._current_path = path
    _with_current_path = _generative()(_with_current_path)

    def with_polymorphic(self, cls_or_mappers, selectable=None):
        """Load columns for descendant mappers of this Query's mapper.

        Using this method will ensure that each descendant mapper's
        tables are included in the FROM clause, and will allow filter()
        criterion to be used against those tables.  The resulting
        instances will also have those columns already loaded so that
        no "post fetch" of those columns will be required.

        ``cls_or_mappers`` is a single class or mapper, or list of class/mappers,
        which inherit from this Query's mapper.  Alternatively, it
        may also be the string ``'*'``, in which case all descending
        mappers will be added to the FROM clause.

        ``selectable`` is a table or select() statement that will
        be used in place of the generated FROM clause.  This argument
        is required if any of the desired mappers use concrete table
        inheritance, since SQLAlchemy currently cannot generate UNIONs
        among tables automatically.  If used, the ``selectable``
        argument must represent the full set of tables and columns mapped
        by every desired mapper.  Otherwise, the unaccounted mapped columns
        will result in their table being appended directly to the FROM
        clause which will usually lead to incorrect results.

        """
        entity = self._generate_mapper_zero()
        entity.set_with_polymorphic(self, cls_or_mappers, selectable=selectable)
    with_polymorphic = _generative(__no_from_condition, __no_criterion_condition)(with_polymorphic)

    def yield_per(self, count):
        """Yield only ``count`` rows at a time.

        WARNING: use this method with caution; if the same instance is present
        in more than one batch of rows, end-user changes to attributes will be
        overwritten.

        In particular, it's usually impossible to use this setting with
        eagerly loaded collections (i.e. any lazy=False) since those
        collections will be cleared for a new load when encountered in a
        subsequent result batch.

        """
        self._yield_per = count
    yield_per = _generative()(yield_per)

    def get(self, ident, **kwargs):
        """Return an instance of the object based on the given identifier, or None if not found.

        The `ident` argument is a scalar or tuple of primary key column values
        in the order of the table def's primary key columns.

        """

        ret = self._extension_zero().get(self, ident, **kwargs)
        if ret is not mapper.EXT_CONTINUE:
            return ret

        # convert composite types to individual args
        if hasattr(ident, '__composite_values__'):
            ident = ident.__composite_values__()

        key = self._only_mapper_zero().identity_key_from_primary_key(ident)
        return self._get(key, ident, **kwargs)

    def load(self, ident, raiseerr=True, **kwargs):
        """Return an instance of the object based on the given identifier.

        If not found, raises an exception.  The method will **remove all
        pending changes** to the object already existing in the Session.  The
        `ident` argument is a scalar or tuple of primary key column values in
        the order of the table def's primary key columns.

        """
        ret = self._extension_zero().load(self, ident, **kwargs)
        if ret is not mapper.EXT_CONTINUE:
            return ret

        # convert composite types to individual args
        if hasattr(ident, '__composite_values__'):
            ident = ident.__composite_values__()

        key = self._only_mapper_zero().identity_key_from_primary_key(ident)
        instance = self.populate_existing()._get(key, ident, **kwargs)
        if instance is None and raiseerr:
            raise sa_exc.InvalidRequestError("No instance found for identity %s" % repr(ident))
        return instance

    def query_from_parent(cls, instance, property, **kwargs):
        """Return a new Query with criterion corresponding to a parent instance.

        Return a newly constructed Query object, with criterion corresponding
        to a relationship to the given parent instance.

        instance
          a persistent or detached instance which is related to class
          represented by this query.

         property
           string name of the property which relates this query's class to the
           instance.

         \**kwargs
           all extra keyword arguments are propagated to the constructor of
           Query.

       deprecated.  use sqlalchemy.orm.with_parent in conjunction with
       filter().

        """
        mapper = object_mapper(instance)
        prop = mapper.get_property(property, resolve_synonyms=True)
        target = prop.mapper
        criterion = prop.compare(operators.eq, instance, value_is_parent=True)
        return Query(target, **kwargs).filter(criterion)
    query_from_parent = classmethod(util.deprecated(None, False)(query_from_parent))

    def correlate(self, *args):
        self._correlate = self._correlate.union([_orm_selectable(s) for s in args])
    correlate = _generative()(correlate)

    def autoflush(self, setting):
        """Return a Query with a specific 'autoflush' setting.

        Note that a Session with autoflush=False will
        not autoflush, even if this flag is set to True at the
        Query level.  Therefore this flag is usually used only
        to disable autoflush for a specific Query.

        """
        self._autoflush = setting
    autoflush = _generative()(autoflush)

    def populate_existing(self):
        """Return a Query that will refresh all instances loaded.

        This includes all entities accessed from the database, including
        secondary entities, eagerly-loaded collection items.

        All changes present on entities which are already present in the
        session will be reset and the entities will all be marked "clean".

        An alternative to populate_existing() is to expire the Session
        fully using session.expire_all().

        """
        self._populate_existing = True
    populate_existing = _generative()(populate_existing)

    def with_parent(self, instance, property=None):
        """add a join criterion corresponding to a relationship to the given parent instance.

            instance
                a persistent or detached instance which is related to class represented
                by this query.

            property
                string name of the property which relates this query's class to the
                instance.  if None, the method will attempt to find a suitable property.

        currently, this method only works with immediate parent relationships, but in the
        future may be enhanced to work across a chain of parent mappers.

        """
        from sqlalchemy.orm import properties
        mapper = object_mapper(instance)
        if property is None:
            for prop in mapper.iterate_properties:
                if isinstance(prop, properties.PropertyLoader) and prop.mapper is self._mapper_zero():
                    break
            else:
                raise sa_exc.InvalidRequestError("Could not locate a property which relates instances of class '%s' to instances of class '%s'" % (self._mapper_zero().class_.__name__, instance.__class__.__name__))
        else:
            prop = mapper.get_property(property, resolve_synonyms=True)
        return self.filter(prop.compare(operators.eq, instance, value_is_parent=True))

    def add_entity(self, entity, alias=None):
        """add a mapped entity to the list of result columns to be returned."""

        if alias:
            entity = aliased(entity, alias)

        self._entities = list(self._entities)
        m = _MapperEntity(self, entity)
        self.__setup_aliasizers([m])
    add_entity = _generative()(add_entity)

    def from_self(self, *entities):
        """return a Query that selects from this Query's SELECT statement.

        \*entities - optional list of entities which will replace
        those being selected.
        """

        fromclause = self.with_labels().statement.correlate(None)
        self._statement = self._criterion = None
        self._order_by = self._group_by = self._distinct = False
        self._limit = self._offset = None
        self.__set_select_from(fromclause)
        if entities:
            self._entities = []
            for ent in entities:
                _QueryEntity(self, ent)
            self.__setup_aliasizers(self._entities)

    from_self = _generative()(from_self)
    _from_self = from_self

    def values(self, *columns):
        """Return an iterator yielding result tuples corresponding to the given list of columns"""

        if not columns:
            return iter(())
        q = self._clone()
        q._entities = []
        for column in columns:
            _ColumnEntity(q, column)
        q.__setup_aliasizers(q._entities)
        if not q._yield_per:
            q._yield_per = 10
        return iter(q)
    _values = values

    def add_column(self, column):
        """Add a SQL ColumnElement to the list of result columns to be returned."""

        self._entities = list(self._entities)
        c = _ColumnEntity(self, column)
        self.__setup_aliasizers([c])
    add_column = _generative()(add_column)

    def options(self, *args):
        """Return a new Query object, applying the given list of
        MapperOptions.

        """
        return self.__options(False, *args)

    def _conditional_options(self, *args):
        return self.__options(True, *args)

    def __options(self, conditional, *args):
        # most MapperOptions write to the '_attributes' dictionary,
        # so copy that as well
        self._attributes = self._attributes.copy()
        opts = [o for o in util.flatten_iterator(args)]
        self._with_options = self._with_options + opts
        if conditional:
            for opt in opts:
                opt.process_query_conditionally(self)
        else:
            for opt in opts:
                opt.process_query(self)
    __options = _generative()(__options)

    def with_lockmode(self, mode):
        """Return a new Query object with the specified locking mode."""

        self._lockmode = mode
    with_lockmode = _generative()(with_lockmode)

    def params(self, *args, **kwargs):
        """add values for bind parameters which may have been specified in filter().

        parameters may be specified using \**kwargs, or optionally a single dictionary
        as the first positional argument.  The reason for both is that \**kwargs is
        convenient, however some parameter dictionaries contain unicode keys in which case
        \**kwargs cannot be used.

        """
        if len(args) == 1:
            kwargs.update(args[0])
        elif len(args) > 0:
            raise sa_exc.ArgumentError("params() takes zero or one positional argument, which is a dictionary.")
        self._params = self._params.copy()
        self._params.update(kwargs)
    params = _generative()(params)

    def filter(self, criterion):
        """apply the given filtering criterion to the query and return the newly resulting ``Query``

        the criterion is any sql.ClauseElement applicable to the WHERE clause of a select.

        """
        if isinstance(criterion, basestring):
            criterion = sql.text(criterion)

        if criterion is not None and not isinstance(criterion, sql.ClauseElement):
            raise sa_exc.ArgumentError("filter() argument must be of type sqlalchemy.sql.ClauseElement or string")

        criterion = self._adapt_clause(criterion, True, True)

        if self._criterion is not None:
            self._criterion = self._criterion & criterion
        else:
            self._criterion = criterion
    filter = _generative(__no_statement_condition, __no_limit_offset)(filter)

    def filter_by(self, **kwargs):
        """apply the given filtering criterion to the query and return the newly resulting ``Query``."""

        clauses = [_entity_descriptor(self._joinpoint_zero(), key)[0] == value
            for key, value in kwargs.iteritems()]

        return self.filter(sql.and_(*clauses))


    def min(self, col):
        """Execute the SQL ``min()`` function against the given column."""

        return self._col_aggregate(col, sql.func.min)

    def max(self, col):
        """Execute the SQL ``max()`` function against the given column."""

        return self._col_aggregate(col, sql.func.max)

    def sum(self, col):
        """Execute the SQL ``sum()`` function against the given column."""

        return self._col_aggregate(col, sql.func.sum)

    def avg(self, col):
        """Execute the SQL ``avg()`` function against the given column."""

        return self._col_aggregate(col, sql.func.avg)

    def order_by(self, *criterion):
        """apply one or more ORDER BY criterion to the query and return the newly resulting ``Query``"""

        criterion = [self._adapt_clause(expression._literal_as_text(o), True, True) for o in criterion]

        if self._order_by is False:
            self._order_by = criterion
        else:
            self._order_by = self._order_by + criterion
    order_by = util.array_as_starargs_decorator(order_by)
    order_by = _generative(__no_statement_condition, __no_limit_offset)(order_by)

    def group_by(self, *criterion):
        """apply one or more GROUP BY criterion to the query and return the newly resulting ``Query``"""

        criterion = list(chain(*[_orm_columns(c) for c in criterion]))

        if self._group_by is False:
            self._group_by = criterion
        else:
            self._group_by = self._group_by + criterion
    group_by = util.array_as_starargs_decorator(group_by)
    group_by = _generative(__no_statement_condition, __no_limit_offset)(group_by)

    def having(self, criterion):
        """apply a HAVING criterion to the query and return the newly resulting ``Query``."""

        if isinstance(criterion, basestring):
            criterion = sql.text(criterion)

        if criterion is not None and not isinstance(criterion, sql.ClauseElement):
            raise sa_exc.ArgumentError("having() argument must be of type sqlalchemy.sql.ClauseElement or string")

        criterion = self._adapt_clause(criterion, True, True)

        if self._having is not None:
            self._having = self._having & criterion
        else:
            self._having = criterion
    having = _generative(__no_statement_condition, __no_limit_offset)(having)

    def join(self, *props, **kwargs):
        """Create a join against this ``Query`` object's criterion
        and apply generatively, returning the newly resulting ``Query``.

        each element in \*props may be:
        
          * a string property name, i.e. "rooms".  This will join along
            the relation of the same name from this Query's "primary"
            mapper, if one is present.
          
          * a class-mapped attribute, i.e. Houses.rooms.  This will create a
            join from "Houses" table to that of the "rooms" relation.
          
          * a 2-tuple containing a target class or selectable, and 
            an "ON" clause.  The ON clause can be the property name/
            attribute like above, or a SQL expression.
          
          
        e.g.::

            # join along string attribute names
            session.query(Company).join('employees')
            session.query(Company).join('employees', 'tasks')

            # join the Person entity to an alias of itself,
            # along the "friends" relation
            PAlias = aliased(Person)
            session.query(Person).join((Palias, Person.friends))

            # join from Houses to the "rooms" attribute on the
            # "Colonials" subclass of Houses, then join to the 
            # "closets" relation on Room
            session.query(Houses).join(Colonials.rooms, Room.closets)
            
            # join from Company entities to the "employees" collection,
            # using "people JOIN engineers" as the target.  Then join
            # to the "computers" collection on the Engineer entity.
            session.query(Company).join((people.join(engineers), 'employees'), Engineer.computers)
            
            # join from Articles to Keywords, using the "keywords" attribute.
            # assume this is a many-to-many relation.
            session.query(Article).join(Article.keywords)
            
            # same thing, but spelled out entirely explicitly 
            # including the association table.
            session.query(Article).join(
                (article_keywords, Articles.id==article_keywords.c.article_id),
                (Keyword, Keyword.id==article_keywords.c.keyword_id)
                )

        \**kwargs include:

            aliased - when joining, create anonymous aliases of each table.  This is
            used for self-referential joins or multiple joins to the same table.
            Consider usage of the aliased(SomeClass) construct as a more explicit
            approach to this.

            from_joinpoint - when joins are specified using string property names,
            locate the property from the mapper found in the most recent previous 
            join() call, instead of from the root entity.

        """
        aliased, from_joinpoint = kwargs.pop('aliased', False), kwargs.pop('from_joinpoint', False)
        if kwargs:
            raise TypeError("unknown arguments: %s" % ','.join(kwargs.keys()))
        return self.__join(props, outerjoin=False, create_aliases=aliased, from_joinpoint=from_joinpoint)
    join = util.array_as_starargs_decorator(join)

    def outerjoin(self, *props, **kwargs):
        """Create a left outer join against this ``Query`` object's criterion
        and apply generatively, retunring the newly resulting ``Query``.
        
        Usage is the same as the ``join()`` method.

        """
        aliased, from_joinpoint = kwargs.pop('aliased', False), kwargs.pop('from_joinpoint', False)
        if kwargs:
            raise TypeError("unknown arguments: %s" % ','.join(kwargs.keys()))
        return self.__join(props, outerjoin=True, create_aliases=aliased, from_joinpoint=from_joinpoint)
    outerjoin = util.array_as_starargs_decorator(outerjoin)

    def __join(self, keys, outerjoin, create_aliases, from_joinpoint):
        self.__currenttables = util.Set(self.__currenttables)
        self._polymorphic_adapters = self._polymorphic_adapters.copy()

        if not from_joinpoint:
            self.__reset_joinpoint()

        clause = self._from_obj
        right_entity = None

        for arg1 in util.to_list(keys):
            prop =  None
            aliased_entity = False
            alias_criterion = False
            left_entity = right_entity
            right_entity = right_mapper = None
            
            if isinstance(arg1, tuple):
                arg1, arg2 = arg1
            else:
                arg2 = None
            
            if isinstance(arg2, (interfaces.PropComparator, basestring)):
                onclause = arg2
                right_entity = arg1
            elif isinstance(arg1, (interfaces.PropComparator, basestring)):
                onclause = arg1
                right_entity = arg2 
            else:
                onclause = arg2
                right_entity = arg1

            if isinstance(onclause, interfaces.PropComparator):
                of_type = getattr(onclause, '_of_type', None)
                prop = onclause.property
                descriptor = onclause
                
                if not left_entity:
                    left_entity = onclause.parententity
                    
                if of_type:
                    right_mapper = of_type
                else:
                    right_mapper = prop.mapper
                    
                if not right_entity:
                    right_entity = right_mapper
                    
            elif isinstance(onclause, basestring):
                if not left_entity:
                    left_entity = self._joinpoint_zero()
                    
                descriptor, prop = _entity_descriptor(left_entity, onclause)
                right_mapper = prop.mapper
                if not right_entity:
                    right_entity = right_mapper
            elif onclause is None:
                if not left_entity:
                    left_entity = self._joinpoint_zero()
            else:
                if not left_entity:
                    left_entity = self._joinpoint_zero()
                    
            if not clause:
                if isinstance(onclause, interfaces.PropComparator):
                    clause = onclause.__clause_element__()

                for ent in self._mapper_entities:
                    if ent.corresponds_to(left_entity):
                        clause = ent.selectable
                        break

            if not clause:
                raise exc.InvalidRequestError("Could not find a FROM clause to join from")

            bogus, right_selectable, is_aliased_class = _entity_info(right_entity)

            if right_mapper and not is_aliased_class:
                if right_entity is right_selectable:

                    if not right_selectable.is_derived_from(right_mapper.mapped_table):
                        raise sa_exc.InvalidRequestError("Selectable '%s' is not derived from '%s'" % (right_selectable.description, right_mapper.mapped_table.description))

                    if not isinstance(right_selectable, expression.Alias):
                        right_selectable = right_selectable.alias()

                    right_entity = aliased(right_mapper, right_selectable)
                    alias_criterion = True

                elif right_mapper.with_polymorphic or isinstance(right_mapper.mapped_table, expression.Join):
                    aliased_entity = True
                    right_entity = aliased(right_mapper)
                    alias_criterion = True
                
                elif create_aliases:
                    right_entity = aliased(right_mapper)
                    alias_criterion = True
                    
                elif prop:
                    if prop.table in self.__currenttables:
                        if prop.secondary is not None and prop.secondary not in self.__currenttables:
                            # TODO: this check is not strong enough for different paths to the same endpoint which
                            # does not use secondary tables
                            raise sa_exc.InvalidRequestError("Can't join to property '%s'; a path to this table along a different secondary table already exists.  Use the `alias=True` argument to `join()`." % descriptor)

                        continue

                    if prop.secondary:
                        self.__currenttables.add(prop.secondary)
                    self.__currenttables.add(prop.table)

                    right_entity = prop.mapper

            if prop:
                onclause = prop
            
            clause = orm_join(clause, right_entity, onclause, isouter=outerjoin)
            if alias_criterion: 
                self._filter_aliases = ORMAdapter(right_entity, 
                        equivalents=right_mapper._equivalent_columns, chain_to=self._filter_aliases)

                if aliased_entity:
                    self.__mapper_loads_polymorphically_with(right_mapper, ORMAdapter(right_entity, equivalents=right_mapper._equivalent_columns))

        self._from_obj = clause
        self._joinpoint = right_entity

    __join = _generative(__no_statement_condition, __no_limit_offset)(__join)

    def reset_joinpoint(self):
        """return a new Query reset the 'joinpoint' of this Query reset
        back to the starting mapper.  Subsequent generative calls will
        be constructed from the new joinpoint.

        Note that each call to join() or outerjoin() also starts from
        the root.

        """
        self.__reset_joinpoint()
    reset_joinpoint = _generative(__no_statement_condition)(reset_joinpoint)

    def select_from(self, from_obj):
        """Set the `from_obj` parameter of the query and return the newly
        resulting ``Query``.  This replaces the table which this Query selects
        from with the given table.


        `from_obj` is a single table or selectable.

        """
        if isinstance(from_obj, (tuple, list)):
            util.warn_deprecated("select_from() now accepts a single Selectable as its argument, which replaces any existing FROM criterion.")
            from_obj = from_obj[-1]
        
        self.__set_select_from(from_obj)
    select_from = _generative(__no_from_condition, __no_criterion_condition)(select_from)

    def __getitem__(self, item):
        if isinstance(item, slice):
            start, stop, step = util.decode_slice(item)
            # if we slice from the end we need to execute the query
            if start < 0 or stop < 0:
                return list(self)[item]
            else:
                res = self.slice(start, stop)
                if step is not None:
                    return list(res)[None:None:item.step]
                else:
                    return list(res)
        else:
            return list(self[item:item+1])[0]
    
    def slice(self, start, stop):
        """apply LIMIT/OFFSET to the ``Query`` based on a range and return the newly resulting ``Query``."""
        
        if start is not None and stop is not None:
            self._offset = (self._offset or 0) + start
            self._limit = stop - start
        elif start is None and stop is not None:
            self._limit = stop
        elif start is not None and stop is None:
            self._offset = (self._offset or 0) + start
    slice = _generative(__no_statement_condition)(slice)
        
    def limit(self, limit):
        """Apply a ``LIMIT`` to the query and return the newly resulting

        ``Query``.

        """
        
        self._limit = limit
    limit = _generative(__no_statement_condition)(limit)
    
    def offset(self, offset):
        """Apply an ``OFFSET`` to the query and return the newly resulting
        ``Query``.

        """
        
        self._offset = offset
    offset = _generative(__no_statement_condition)(offset)
    
    def distinct(self):
        """Apply a ``DISTINCT`` to the query and return the newly resulting
        ``Query``.

        """
        self._distinct = True
    distinct = _generative(__no_statement_condition)(distinct)

    def all(self):
        """Return the results represented by this ``Query`` as a list.

        This results in an execution of the underlying query.

        """
        return list(self)

    def from_statement(self, statement):
        """Execute the given SELECT statement and return results.

        This method bypasses all internal statement compilation, and the
        statement is executed without modification.

        The statement argument is either a string, a ``select()`` construct,
        or a ``text()`` construct, and should return the set of columns
        appropriate to the entity class represented by this ``Query``.

        Also see the ``instances()`` method.

        """
        if isinstance(statement, basestring):
            statement = sql.text(statement)
        self._statement = statement
    from_statement = _generative(__no_criterion_condition)(from_statement)

    def first(self):
        """Return the first result of this ``Query`` or None if the result doesn't contain any row.

        This results in an execution of the underlying query.

        """
        if self._statement:
            return list(self)[0]
        else:
            ret = list(self[0:1])
            if len(ret) > 0:
                return ret[0]
            else:
                return None

    def one(self):
        """Return exactly one result or raise an exception.

        Raises ``sqlalchemy.orm.NoResultError`` if the query selects no rows.
        Raisees ``sqlalchemy.orm.MultipleResultsError`` if multiple rows are
        selected.

        This results in an execution of the underlying query.

        """
        if self._statement:
            raise exceptions.InvalidRequestError(
                "one() not available when from_statement() is used; "
                "use `first()` instead.")

        ret = list(self[0:2])

        if len(ret) == 1:
            return ret[0]
        elif len(ret) == 0:
            raise orm_exc.NoResultFound("No row was found for one()")
        else:
            raise orm_exc.MultipleResultsFound(
                "Multiple rows were found for one()")

    def __iter__(self):
        context = self._compile_context()
        context.statement.use_labels = True
        if self._autoflush and not self._populate_existing:
            self.session._autoflush()
        return self._execute_and_instances(context)

    def _execute_and_instances(self, querycontext):
        result = self.session.execute(querycontext.statement, params=self._params, mapper=self._mapper_zero_or_none(), _state=self._refresh_state)
        return self.iterate_instances(result, querycontext)

    def instances(self, cursor, __context=None):
        return list(self.iterate_instances(cursor, __context))

    def iterate_instances(self, cursor, __context=None):
        session = self.session

        context = __context
        if context is None:
            context = QueryContext(self)

        context.runid = _new_runid()

        filtered = bool(list(self._mapper_entities))
        single_entity = filtered and len(self._entities) == 1

        if filtered:
            if single_entity:
                filter = util.OrderedIdentitySet
            else:
                filter = util.OrderedSet
        else:
            filter = None

        custom_rows = single_entity and 'append_result' in self._entities[0].extension.methods

        (process, labels) = zip(*[query_entity.row_processor(self, context, custom_rows) for query_entity in self._entities])

        if not single_entity:
            labels = dict([(label, property(util.itemgetter(i))) for i, label in enumerate(labels) if label])
            rowtuple = type.__new__(type, "RowTuple", (tuple,), labels)
            rowtuple.keys = labels.keys
            
        while True:
            context.progress = util.Set()
            context.partials = {}

            if self._yield_per:
                fetch = cursor.fetchmany(self._yield_per)
                if not fetch:
                    break
            else:
                fetch = cursor.fetchall()
            
            if custom_rows:
                rows = []
                for row in fetch:
                    process[0](context, row, rows)
            elif single_entity:
                rows = [process[0](context, row) for row in fetch]
            else:
                rows = [rowtuple([proc(context, row) for proc in process]) for row in fetch]

            if filter:
                rows = filter(rows)

            if context.refresh_state and self._only_load_props and context.refresh_state in context.progress:
                context.refresh_state.commit(self._only_load_props)
                context.progress.remove(context.refresh_state)

            session._finalize_loaded(context.progress)

            for ii, attrs in context.partials.items():
                ii.commit(attrs)

            for row in rows:
                yield row

            if not self._yield_per:
                break

    def _get(self, key=None, ident=None, refresh_state=None, lockmode=None, only_load_props=None):
        lockmode = lockmode or self._lockmode
        if not self._populate_existing and not refresh_state and not self._mapper_zero().always_refresh and lockmode is None:
            try:
                instance = self.session.identity_map[key]
                state = attributes.instance_state(instance)
                if state.expired:
                    try:
                        state()
                    except orm_exc.ObjectDeletedError:
                        # TODO: should we expunge ?  if so, should we expunge here ? or in mapper._load_scalar_attributes ?
                        self.session.expunge(instance)
                        return None
                return instance
            except KeyError:
                pass

        if ident is None:
            if key is not None:
                ident = key[1]
        else:
            ident = util.to_list(ident)

        if refresh_state is None:
            q = self.__no_criterion()
        else:
            q = self._clone()

        if ident is not None:
            mapper = q._mapper_zero()
            params = {}
            (_get_clause, _get_params) = mapper._get_clause

            _get_clause = q._adapt_clause(_get_clause, True, False)
            q._criterion = _get_clause

            for i, primary_key in enumerate(mapper.primary_key):
                try:
                    params[_get_params[primary_key].key] = ident[i]
                except IndexError:
                    raise sa_exc.InvalidRequestError("Could not find enough values to formulate primary key for query.get(); primary key columns are %s" % ', '.join(["'%s'" % str(c) for c in q.mapper.primary_key]))
            q._params = params

        if lockmode is not None:
            q._lockmode = lockmode
        q.__get_options(populate_existing=bool(refresh_state), version_check=(lockmode is not None), only_load_props=only_load_props, refresh_state=refresh_state)
        q._order_by = None
        try:
            # call using all() to avoid LIMIT compilation complexity
            return q.all()[0]
        except IndexError:
            return None

    def _select_args(self):
        return {'limit':self._limit, 'offset':self._offset, 'distinct':self._distinct, 'group_by':self._group_by or None, 'having':self._having or None}
    _select_args = property(_select_args)

    def _should_nest_selectable(self):
        kwargs = self._select_args
        return (kwargs.get('limit') is not None or kwargs.get('offset') is not None or kwargs.get('distinct', False))
    _should_nest_selectable = property(_should_nest_selectable)

    def count(self):
        """Apply this query's criterion to a SELECT COUNT statement.

        this is the purely generative version which will become
        the public method in version 0.5.

        """
        return self._col_aggregate(sql.literal_column('1'), sql.func.count, nested_cols=list(self._mapper_zero().primary_key))

    def _col_aggregate(self, col, func, nested_cols=None):
        whereclause = self._criterion

        context = QueryContext(self)
        from_obj = self.__mapper_zero_from_obj()

        if self._should_nest_selectable:
            if not nested_cols:
                nested_cols = [col]
            s = sql.select(nested_cols, whereclause, from_obj=from_obj, **self._select_args)
            s = s.alias()
            s = sql.select([func(s.corresponding_column(col) or col)]).select_from(s)
        else:
            s = sql.select([func(col)], whereclause, from_obj=from_obj, **self._select_args)

        if self._autoflush and not self._populate_existing:
            self.session._autoflush()
        return self.session.scalar(s, params=self._params, mapper=self._mapper_zero())
    
    def delete(self, synchronize_session='evaluate'):
        """EXPERIMENTAL"""
        #TODO: lots of duplication and ifs - probably needs to be refactored to strategies
        context = self._compile_context()
        if len(context.statement.froms) != 1 or not isinstance(context.statement.froms[0], schema.Table):
            raise sa_exc.ArgumentError("Only deletion via a single table query is currently supported")
        primary_table = context.statement.froms[0]
        
        session = self.session
        
        if synchronize_session == 'evaluate':
            try:
                evaluator_compiler = evaluator.EvaluatorCompiler()
                eval_condition = evaluator_compiler.process(self.whereclause)
            except evaluator.UnevaluatableError:
                synchronize_session = 'fetch'
        
        delete_stmt = sql.delete(primary_table, context.whereclause)
        
        if synchronize_session == 'fetch':
            #TODO: use RETURNING when available
            select_stmt = context.statement.with_only_columns(primary_table.primary_key)
            matched_rows = session.execute(select_stmt).fetchall()
        
        session.execute(delete_stmt)
        
        if synchronize_session == 'evaluate':
            target_cls = self._mapper_zero().class_
            
            #TODO: detect when the where clause is a trivial primary key match
            objs_to_expunge = [obj for (cls, pk, entity_name),obj in session.identity_map.iteritems()
                if issubclass(cls, target_cls) and eval_condition(obj)]
            for obj in objs_to_expunge:
                session.expunge(obj)
        elif synchronize_session == 'fetch':
            target_mapper = self._mapper_zero()
            for primary_key in matched_rows:
                identity_key = target_mapper.identity_key_from_primary_key(list(primary_key))
                if identity_key in session.identity_map:
                    session.expunge(session.identity_map[identity_key])

    def update(self, values, synchronize_session='evaluate'):
        """EXPERIMENTAL"""
        
        #TODO: value keys need to be mapped to corresponding sql cols and instr.attr.s to string keys
        #TODO: updates of manytoone relations need to be converted to fk assignments
        
        context = self._compile_context()
        if len(context.statement.froms) != 1 or not isinstance(context.statement.froms[0], schema.Table):
            raise sa_exc.ArgumentError("Only update via a single table query is currently supported")
        primary_table = context.statement.froms[0]
        
        session = self.session
        
        if synchronize_session == 'evaluate':
            try:
                evaluator_compiler = evaluator.EvaluatorCompiler()
                eval_condition = evaluator_compiler.process(self.whereclause)
                
                value_evaluators = {}
                for key,value in values.items():
                    value_evaluators[key] = evaluator_compiler.process(expression._literal_as_binds(value))
            except evaluator.UnevaluatableError:
                synchronize_session = 'expire'
        
        update_stmt = sql.update(primary_table, context.whereclause, values)
        
        if synchronize_session == 'expire':
            select_stmt = context.statement.with_only_columns(primary_table.primary_key)
            matched_rows = session.execute(select_stmt).fetchall()
        
        session.execute(update_stmt)
        
        if synchronize_session == 'evaluate':
            target_cls = self._mapper_zero().class_
            
            for (cls, pk, entity_name),obj in session.identity_map.iteritems():
                if issubclass(cls, target_cls) and eval_condition(obj):
                    for key,eval_value in value_evaluators.items():
                        obj.__dict__[key] = eval_value(obj)
        
        elif synchronize_session == 'expire':
            target_mapper = self._mapper_zero()
            
            for primary_key in matched_rows:
                identity_key = target_mapper.identity_key_from_primary_key(list(primary_key))
                if identity_key in session.identity_map:
                    session.expire(session.identity_map[identity_key], values.keys())
       
    
    def _compile_context(self, labels=True):
        context = QueryContext(self)

        if context.statement:
            return context

        if self._lockmode:
            try:
                for_update = {'read': 'read',
                              'update': True,
                              'update_nowait': 'nowait',
                              None: False}[self._lockmode]
            except KeyError:
                raise sa_exc.ArgumentError("Unknown lockmode '%s'" % self._lockmode)
        else:
            for_update = False

        for entity in self._entities:
            entity.setup_context(self, context)

        eager_joins = context.eager_joins.values()

        if context.from_clause:
            froms = [context.from_clause]  # "load from a single FROM" mode, i.e. when select_from() or join() is used
        else:
            froms = context.froms   # "load from discrete FROMs" mode, i.e. when each _MappedEntity has its own FROM

        if eager_joins and self._should_nest_selectable:
            # for eager joins present and LIMIT/OFFSET/DISTINCT, wrap the query inside a select,
            # then append eager joins onto that

            if context.order_by:
                order_by_col_expr = list(chain(*[sql_util.find_columns(o) for o in context.order_by]))
            else:
                context.order_by = None
                order_by_col_expr = []

            inner = sql.select(context.primary_columns + order_by_col_expr, context.whereclause, from_obj=froms, use_labels=labels, correlate=False, order_by=context.order_by, **self._select_args)

            if self._correlate:
                inner = inner.correlate(*self._correlate)

            inner = inner.alias()

            equivs = self.__all_equivs()

            context.adapter = sql_util.ColumnAdapter(inner, equivs)

            statement = sql.select([inner] + context.secondary_columns, for_update=for_update, use_labels=labels)
            
            from_clause = inner
            for eager_join in eager_joins:
                # EagerLoader places a 'stop_on' attribute on the join, 
                # giving us a marker as to where the "splice point" of the join should be
                from_clause = sql_util.splice_joins(from_clause, eager_join, eager_join.stop_on)

            statement.append_from(from_clause)

            if context.order_by:
                local_adapter = sql_util.ClauseAdapter(inner)
                statement.append_order_by(*local_adapter.copy_and_process(context.order_by))

            statement.append_order_by(*context.eager_order_by)
        else:
            if not context.order_by:
                context.order_by = None

            if self._distinct and context.order_by:
                order_by_col_expr = list(chain(*[sql_util.find_columns(o) for o in context.order_by]))
                context.primary_columns += order_by_col_expr

            froms += context.eager_joins.values()

            statement = sql.select(context.primary_columns + context.secondary_columns, context.whereclause, from_obj=froms, use_labels=labels, for_update=for_update, correlate=False, order_by=context.order_by, **self._select_args)
            if self._correlate:
                statement = statement.correlate(*self._correlate)

            if context.eager_order_by:
                statement.append_order_by(*context.eager_order_by)
                
        context.statement = statement
        
        return context

    def __log_debug(self, msg):
        self.logger.debug(msg)

    def __str__(self):
        return str(self.compile())


class _QueryEntity(object):
    """represent an entity column returned within a Query result."""

    def __new__(cls, *args, **kwargs):
        if cls is _QueryEntity:
            entity = args[1]
            if _is_mapped_class(entity):
                cls = _MapperEntity
            else:
                cls = _ColumnEntity
        return object.__new__(cls)

    def _clone(self):
        q = self.__class__.__new__(self.__class__)
        q.__dict__ = self.__dict__.copy()
        return q

class _MapperEntity(_QueryEntity):
    """mapper/class/AliasedClass entity"""

    def __init__(self, query, entity, entity_name=None):
        self.primary_entity = not query._entities
        query._entities.append(self)

        self.entities = [entity]
        self.entity_zero = entity
        self.entity_name = entity_name

    def setup_entity(self, entity, mapper, adapter, from_obj, is_aliased_class, with_polymorphic):
        self.mapper = mapper
        self.extension = self.mapper.extension
        self.adapter = adapter
        self.selectable  = from_obj
        self._with_polymorphic = with_polymorphic
        self.is_aliased_class = is_aliased_class
        if is_aliased_class:
            self.path_entity = self.entity = self.entity_zero = entity
        else:
            self.path_entity = mapper.base_mapper
            self.entity = self.entity_zero = mapper

    def set_with_polymorphic(self, query, cls_or_mappers, selectable):
        if cls_or_mappers is None:
            query._reset_polymorphic_adapter(self.mapper)
            return
        
        mappers, from_obj = self.mapper._with_polymorphic_args(cls_or_mappers, selectable)
        self._with_polymorphic = mappers

        # TODO: do the wrapped thing here too so that with_polymorphic() can be
        # applied to aliases
        if not self.is_aliased_class:
            self.selectable = from_obj
            self.adapter = query._get_polymorphic_adapter(self, from_obj)

    def corresponds_to(self, entity):
        if _is_aliased_class(entity):
            return entity is self.path_entity
        else:
            return entity.base_mapper is self.path_entity

    def _get_entity_clauses(self, query, context):

        adapter = None
        if not self.is_aliased_class and query._polymorphic_adapters:
            for mapper in self.mapper.iterate_to_root():
                adapter = query._polymorphic_adapters.get(mapper.mapped_table, None)
                if adapter:
                    break

        if not adapter and self.adapter:
            adapter = self.adapter

        if adapter:
            if query._from_obj_alias:
                ret = adapter.wrap(query._from_obj_alias)
            else:
                ret = adapter
        else:
            ret = query._from_obj_alias

        return ret

    def row_processor(self, query, context, custom_rows):
        adapter = self._get_entity_clauses(query, context)

        if context.adapter and adapter:
            adapter = adapter.wrap(context.adapter)
        elif not adapter:
            adapter = context.adapter

        # polymorphic mappers which have concrete tables in their hierarchy usually
        # require row aliasing unconditionally.
        if not adapter and self.mapper._requires_row_aliasing:
            adapter = sql_util.ColumnAdapter(self.selectable, self.mapper._equivalent_columns)

        if self.primary_entity:
            _instance = self.mapper._instance_processor(context, (self.path_entity,), adapter, 
                extension=self.extension, only_load_props=query._only_load_props, refresh_state=context.refresh_state
            )
        else:
            _instance = self.mapper._instance_processor(context, (self.path_entity,), adapter)
        
        if custom_rows:
            def main(context, row, result):
                _instance(row, result)
        else:
            def main(context, row):
                return _instance(row, None)
        
        if self.is_aliased_class:
            entname = self.entity._sa_label_name
        else:
            entname = self.mapper.class_.__name__
            
        return main, entname

    def setup_context(self, query, context):
        # if single-table inheritance mapper, add "typecol IN (polymorphic)" criterion so
        # that we only load the appropriate types
        if self.mapper.single and self.mapper.inherits is not None and self.mapper.polymorphic_on is not None and self.mapper.polymorphic_identity is not None:
            context.whereclause = sql.and_(context.whereclause, self.mapper.polymorphic_on.in_([m.polymorphic_identity for m in self.mapper.polymorphic_iterator()]))

        context.froms.append(self.selectable)

        adapter = self._get_entity_clauses(query, context)

        if context.order_by is False and self.mapper.order_by:
            context.order_by = self.mapper.order_by
                
        if context.order_by and adapter:
            context.order_by = adapter.adapt_list(util.to_list(context.order_by))
            
        for value in self.mapper._iterate_polymorphic_properties(self._with_polymorphic):
            if query._only_load_props and value.key not in query._only_load_props:
                continue
            value.setup(context, self, (self.path_entity,), adapter, only_load_props=query._only_load_props, column_collection=context.primary_columns)

    def __str__(self):
        return str(self.mapper)


class _ColumnEntity(_QueryEntity):
    """Column/expression based entity."""

    def __init__(self, query, column, entity_name=None):
        if isinstance(column, expression.FromClause) and not isinstance(column, expression.ColumnElement):
            for c in column.c:
                _ColumnEntity(query, c)
            return
            
        query._entities.append(self)

        if isinstance(column, basestring):
            column = sql.literal_column(column)
        elif isinstance(column, (attributes.QueryableAttribute, mapper.Mapper._CompileOnAttr)):
            column = column.__clause_element__()
        elif not isinstance(column, sql.ColumnElement):
            raise sa_exc.InvalidRequestError("Invalid column expression '%r'" % column)

        if not hasattr(column, '_label'):
            column = column.label(None)

        self.column = column
        self.entity_name = None
        self.froms = util.Set()
        self.entities = util.OrderedSet([elem._annotations['parententity'] for elem in visitors.iterate(column, {}) if 'parententity' in elem._annotations])
        if self.entities:
            self.entity_zero = list(self.entities)[0]
        else:
            self.entity_zero = None
        
    def setup_entity(self, entity, mapper, adapter, from_obj, is_aliased_class, with_polymorphic):
        self.selectable = from_obj
        self.froms.add(from_obj)

    def __resolve_expr_against_query_aliases(self, query, expr, context):
        return query._adapt_clause(expr, False, True)

    def row_processor(self, query, context, custom_rows):
        column = self.__resolve_expr_against_query_aliases(query, self.column, context)

        if context.adapter:
            column = context.adapter.columns[column]

        def proc(context, row):
            return row[column]
            
        return (proc, getattr(column, 'name', None))

    def setup_context(self, query, context):
        column = self.__resolve_expr_against_query_aliases(query, self.column, context)
        context.froms += list(self.froms)
        context.primary_columns.append(column)

    def __str__(self):
        return str(self.column)

Query.logger = log.class_logger(Query)

class QueryContext(object):
    def __init__(self, query):

        if query._statement:
            if isinstance(query._statement, expression._SelectBaseMixin) and not query._statement.use_labels:
                self.statement = query._statement.apply_labels()
            else:
                self.statement = query._statement
        else:
            self.statement = None
            self.from_clause = query._from_obj
            self.whereclause = query._criterion
            self.order_by = query._order_by
            if self.order_by:
                self.order_by = [expression._literal_as_text(o) for o in util.to_list(self.order_by)]
            
        self.query = query
        self.session = query.session
        self.populate_existing = query._populate_existing
        self.version_check = query._version_check
        self.refresh_state = query._refresh_state
        self.primary_columns = []
        self.secondary_columns = []
        self.eager_order_by = []

        self.eager_joins = {}
        self.froms = []
        self.adapter = None

        self.options = query._with_options
        self.attributes = query._attributes.copy()

class AliasOption(interfaces.MapperOption):

    def __init__(self, alias):
        self.alias = alias

    def process_query(self, query):
        if isinstance(self.alias, basestring):
            alias = query._mapper_zero().mapped_table.alias(self.alias)
        else:
            alias = self.alias
        query._from_obj_alias = sql_util.ColumnAdapter(alias)
    

_runid = 1L
_id_lock = util.threading.Lock()

def _new_runid():
    global _runid
    _id_lock.acquire()
    try:
        _runid += 1
        return _runid
    finally:
        _id_lock.release()
