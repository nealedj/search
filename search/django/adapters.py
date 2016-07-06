import logging
import operator
from django.db.models import Q as djangoQ
from django.db.models.lookups import Lookup

from ..ql import Q as searchQ


def resolve_filter_value(v):
    """Resolve a filter value to one that the search API can handle

    We can't pass model instances for example.
    """

    return getattr(v, 'pk', v)


class SearchQueryAdapter(object):
    """Adapter class to wrap a `search.query.SearchQuery` instance to behaves
    like a Django queryset.

    We only implement 'enough' to allow it's use within a rest_framework
    viewset and django_filter Filterset.
    """

    def __init__(self, query=None, model=None, queryset=None, _is_none=False):
        self._is_none = _is_none
        self._query = query
        self._queryset = queryset
        self.model = None if queryset is None else queryset.model

    @classmethod
    def from_queryset(cls, queryset):
        if isinstance(queryset, cls):
            return queryset

        filters = cls.get_filters_from_queryset(queryset)

        search_query = cls.filters_to_search_query(filters, queryset.model)

        return cls(search_query, queryset=queryset)

    @classmethod
    def filters_to_search_query(cls, filters, model, query=None):
        """Convert a list of nested lookups filters (a result of get_filters_from_queryset)
        into a SearchQuery objects."""
        search_query = query or model.search_query()
        connector = filters['connector']
        children = filters['children']

        q_objects = None

        for child in children:
            if isinstance(child, tuple):
                q = searchQ(
                    **{
                        "{}__{}".format(child[0], child[1]): child[2]
                    }
                )
                operator_func = getattr(operator, connector.lower() + '_', 'and_')
                q_objects = operator_func(q_objects, q) if q_objects else q

            else:
                search_query = cls.filters_to_search_query(child, model, query=search_query)

        if q_objects is not None:
            # This is essentially a copy of the logic in Query.add_q
            # The trouble is that add_q always ANDs added Q objects but in this case
            # we want to specify the connector ourselves
            if search_query.query._gathered_q is None:
                search_query.query._gathered_q = q_objects
            else:
                search_query.query._gathered_q = getattr(
                    search_query.query._gathered_q,
                    '__{}__'.format(connector.lower())
                )(q_objects)

        return search_query

    @classmethod
    def get_filters_from_queryset(cls, queryset, where_node=None):
        """Translates django queryset filters into a nested dict of tuples

        example:
        queryset = Profile.objects.filter(given_name='pete').filter(Q(email='1@thing.com') | Q(email='2@thing.com'))
        get_filters_from_queryset(queryset)
        returns:
        {
            u'children': [
                    (u'given_name', u'exact', 'pete'),
                    {
                        u'children': [
                            (u'email', u'exact', '1@thing.com'),
                            (u'email', u'exact', '2@thing.com')
                        ],
                        u'connector': u'OR'
                    }
                ],
            u'connector': u'AND'
        }
        """
        where_node = where_node or queryset.query.where

        node_filters = {
            u'connector': unicode(where_node.connector),
        }

        children = []

        for node in where_node.children:
            # Normalize expressions which are an AND with a single child and pull the
            # use the child node as the expression instead.
            # This happens if you add querysets together.
            if getattr(node, 'connector', None) == 'AND' and len(node.children) == 1:
                node = node.children[0]

            if isinstance(node, Lookup):  # Lookup
                children.append(cls.build_lookup(node))

            else:  # WhereNode
                children.append(
                    cls.get_filters_from_queryset(
                        queryset,
                        node,
                    )
                )
        node_filters[u'children'] = children
        return node_filters

    @classmethod
    def build_lookup(cls, node):
        """Converts Django Lookup into a single tuple
        or a list of tuples if the lookup_name is IN

        example for lookup_name IN and rhs ['1@thing.com', '2@thing.com']:
        {
            u'connector': u'OR',
            u'children': [
                (u'email', u'=', u'1@thing.com'),
                (u'email', u'=', u'2@thing.com')
            ]
        }

        example for lookup_name that's not IN (exact in this case) and value '1@thing.com':
        (u'email', u'=', u'1@thing.com')
        """
        target = unicode(node.lhs.target.name)
        lookup_name = unicode(node.lookup_name)

        # convert "IN" into a list of "="
        if lookup_name.lower() == u'in':
            return {
                u'connector': u'OR',
                u'children': [
                    (
                        target,
                        u'exact',
                        value,
                    )
                    for value in node.rhs
                ]
            }

        return (
            target,
            lookup_name,
            node.rhs,
        )

    def _clone(self):
        return self.__class__(
            model=self.model,
            queryset=self._queryset,
            _is_none=self._is_none)

    def _transform_filters(self, *args, **kwargs):
        """Normalize a set of filter kwargs intended for Django queryset to
        those than can be used with a search queryset

        Returns tuple of (args, kwargs) to pass directly to SearchQuery.filter
        """

        _args = [
            self.get_filters_from_queryset(_q) if type(_q) is djangoQ else _q
            for _q in args
        ]
        _kwargs = {k: resolve_filter_value(v) for k, v in kwargs.iteritems()}

        return _args, _kwargs

    def as_model_objects(self):
        """Get the IDs in the order they came back from the search API...
        """
        doc_pks = [int(doc.pk) for doc in self]
        results = (
            self.model.objects
            .filter(id__in=doc_pks)
            .prefetch_related(*self._queryset._prefetch_related_lookups)
        )

        # Since we do pk__in to get the objects from the datastore, we lose
        # any ordering there was. To recreate it, we have to manually order
        # the list back into whatever order the pks from the search API were in
        objects_by_pk = {o.pk: o for o in results}
        results = []
        for pk in doc_pks:
            if pk not in objects_by_pk:
                logging.warning(
                    "%s with PK %d doesn't exist, but search returned it!" % (self.model.__name__, pk),
                )
                continue

            yield objects_by_pk[pk]

    def all(self):
        clone = self._clone()
        clone._query = self._query._clone()
        return clone

    def __len__(self):
        return 0 if self._is_none else self._query.__len__()

    def __iter__(self):
        if self._is_none:
            return iter([])
        else:
            return self._query.__iter__()

    def __getitem__(self, s):
        clone = self._clone()
        clone._query = self._query.__getitem__(s)
        return clone

    def filter(self, *args, **kwargs):
        args, kwargs = self._transform_filters(*args, **kwargs)
        args = args or []

        clone = self._clone()
        clone._query = self._query.filter(*args, **kwargs)

        return clone

    def none(self):
        clone = self._clone()
        clone._is_none = True
        clone._query = self._query._clone()
        return clone

    def count(self):
        return 0 if self._is_none else len(self._query)

    def order_by(self, *fields):
        qs = self._query.order_by(*fields)
        clone = self._clone()
        clone._query = qs
        return clone

