# -*- coding: utf-8 -*-
from django.db import models
from django.db.models.signals import post_save, pre_delete

from djangae import fields
from djangae.test import TestCase

from .. import fields as search_fields, indexers as search_indexers
from ..query import SearchQuery

from .decorators import searchable
from .documents import Document
from .utils import (
    disable_indexing,
    enable_indexing,
    get_uid,
    get_ascii_string_rank
)


class TestUtils(TestCase):

    def test_ascii_rank(self):
        from text_unidecode import unidecode

        strings = [u"a", u"az", u"aaaa", u"azzz", u"zaaa", u"jazz", u"ball", u"a ball", u"łukąźć", u"ołówek", u"♧"]

        ranks = [get_ascii_string_rank(s) for s in strings]

        # Ordering the ranks should result in the same order as the strings.
        self.assertEqual(
            [get_ascii_string_rank(s) for s in sorted([unidecode(s) for s in strings])],
            sorted(ranks)
        )


class Related(models.Model):
    name = models.CharField(max_length=50)


class Foo(models.Model):
    name = models.CharField(max_length=50)
    relation = models.ForeignKey(Related, null=True)
    is_good = models.BooleanField(default=False)
    tags = fields.ListField(models.CharField)


class FooDocument(Document):
    name = search_fields.TextField()
    relation = search_fields.TextField()
    is_good = search_fields.BooleanField()
    tags = search_fields.TextField()

    def build(self, instance):
        self.name = instance.name
        self.relation = str(instance.relation_id)
        self.is_good = instance.is_good
        self.tags = "|".join(instance.tags)


# Emulate decoration
Foo = searchable(FooDocument)(Foo)


class TestSearchable(TestCase):

    def test_decorator_side_effects(self):
        # A signal's receiver list is of the form:
        #
        #   `[((dispatch_uid, some_other_id), receiver), ...]`
        #
        # We test against the dispatch_uid since we know what that should be.
        index_receivers = [
            f[1] for f in post_save.receivers
            if f[0][0] == get_uid(Foo, FooDocument, "django_foo")
        ]
        unindex_receivers = [
            f[1] for f in pre_delete.receivers
            if f[0][0] == get_uid(Foo, FooDocument, "django_foo")
        ]

        self.assertEqual(len(index_receivers), 1)
        self.assertEqual(len(unindex_receivers), 1)
        self.assertTrue(hasattr(Foo, "search_query"))

    def test_search_query_method(self):
        # Only test you can do here really is that it doesn't error... Should
        # probably test to see that the resulting query is bound to the right
        # index and document class somehow
        query = Foo.search_query()
        self.assertEqual(type(query), SearchQuery)

    def test_index_on_save_of_instance(self):
        related1 = Related.objects.create(name="Book")

        thing1 = Foo.objects.create(
            name="Box",
            is_good=False,
            relation=related1,
            tags=["various", "things"]
        )

        related2 = Related.objects.create(name="Book")
        Foo.objects.create(
            name="Crate",
            is_good=False,
            relation=related2,
            tags=["other", "data"]
        )

        query = Foo.search_query().keywords("Box")
        self.assertEqual(query.count(), 1)

        doc = query[0]
        self.assertEqual(doc.doc_id, str(thing1.pk))
        self.assertEqual(doc.pk, str(thing1.pk))
        self.assertEqual(doc.name, "Box")
        self.assertEqual(doc.is_good, False)
        self.assertEqual(doc.relation, str(related1.pk))
        self.assertEqual(doc.tags.split("|"), ["various", "things"])

        # Have to catch an assertion error here that Djangae throws because
        # `Foo` is outside of a registered Django app, so it doesn't know
        # how to uncache it on update. For more info, look at the error.
        try:
            thing1.save()
        except AssertionError:
            pass

        query = Foo.search_query().keywords("Box")
        self.assertEqual(query.count(), 1)

    def test_unindex_on_delete_of_instance(self):
        related = Related.objects.create(name="Book")
        thing = Foo.objects.create(
            name="Box",
            is_good=False,
            relation=related,
            tags=["various", "things"]
        )
        query = Foo.search_query().keywords("Box")
        self.assertEqual(query.count(), 1)

        # Same as above happens on delete...
        try:
            thing.delete()
        except AssertionError:
            pass

        query = Foo.search_query().keywords("Box")
        self.assertEqual(query.count(), 0)

    def test_signals_not_run_when_indexing_disabled(self):
        with disable_indexing():
            related = Related.objects.create(name="Book")
            Foo.objects.create(
                name="Box",
                is_good=False,
                relation=related,
                tags=["various", "things"]
            )

        query = Foo.search_query().keywords("Box")
        self.assertEqual(query.count(), 0)


class FooWithMeta(Foo):
    class SearchMeta:
        fields = ['name', 'name_lower', 'is_good', 'tags', 'relation']
        field_types = {
            'name': search_fields.TextField,
            'name_lower': search_fields.TextField,
            'relation': search_fields.TextField
        }
        field_mappers = {
            'name_lower': lambda o: o.name.lower(),
            'tags': lambda o: u"|".join(o.tags),
            'relation': lambda o: o.relation.name
        }
        corpus = {
            'name': search_indexers.startswith,
            'relation': search_indexers.contains
        }


FooWithMeta = searchable()(FooWithMeta)


class TestSearchableMeta(TestCase):
    def test_metaclass_side_effects(self):
        index_receivers = [
            f[1] for f in post_save.receivers
            if f[0][0] == get_uid(FooWithMeta, "FooWithMetaDocument", "django_foowithmeta")
        ]
        unindex_receivers = [
            f[1] for f in pre_delete.receivers
            if f[0][0] == get_uid(FooWithMeta, "FooWithMetaDocument", "django_foowithmeta")
        ]

        self.assertEqual(len(index_receivers), 1)
        self.assertEqual(len(unindex_receivers), 1)
        self.assertTrue(hasattr(FooWithMeta, "search_query"))

    def test_search_query_method(self):
        query = FooWithMeta.search_query()
        self.assertEqual(type(query), SearchQuery)

    def test_field_types(self):
        document_meta = FooWithMeta._search_meta[1]._doc_meta
        self.assertIsInstance(
            document_meta.fields['name'],
            search_fields.TextField
        )

        self.assertIsInstance(
            document_meta.fields['is_good'],
            search_fields.BooleanField
        )

    def test_index(self):
        document_cls = FooWithMeta._search_meta[1]

        related = Related.objects.create(name=u"Boôk")
        thing1 = FooWithMeta.objects.create(
            name="Big Box",
            is_good=False,
            tags=["various", "things"],
            relation=related
        )

        doc = document_cls(doc_id=str(thing1.pk))
        doc.build_base(thing1)

        self.assertEqual(thing1.name, doc.name)
        self.assertEqual(thing1.name.lower(), doc.name_lower)
        self.assertEqual(thing1.is_good, doc.is_good)
        self.assertEqual(thing1.tags, doc.tags.split("|"))
        self.assertEqual(related.name, related.name)

        corpus = search_indexers.startswith(thing1.name)
        corpus += search_indexers.contains(related.name)
        self.assertEqual(set(corpus), set(doc.corpus.split(' ')))
        self.assertIn(thing1.name, doc.corpus)
        self.assertIn(related.name, doc.corpus)


class TestSearchAdapter(TestCase):
    def setUp(self):
        super(TestSearchAdapter, self).setUp()

        self.related = Related.objects.create(name=u"Boôk")
        self.foo = Foo.objects.create(
            name="Big Box",
            is_good=False,
            tags=["various", "things"],
            relation=self.related
        )


