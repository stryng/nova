from sqlalchemy import CheckConstraint
from sqlalchemy.engine import reflection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import MetaData, Table, Column, Index
from sqlalchemy import select
from sqlalchemy.sql.expression import UpdateBase
from sqlalchemy import Integer, Boolean
from sqlalchemy.types import NullType, BigInteger


all_tables = ['cells']
# note(boris-42): We can't do migration for the dns_domains table because it
#                 doesn't have `id` column.


class InsertFromSelect(UpdateBase):
    def __init__(self, table, select):
        self.table = table
        self.select = select


@compiles(InsertFromSelect)
def visit_insert_from_select(element, compiler, **kw):
    return "INSERT INTO %s %s" % (
        compiler.process(element.table, asfrom=True),
        compiler.process(element.select))


def get_default_deleted_value(table):
    if isinstance(table.c.id.type, Integer):
        return 0
    # NOTE(boris-42): There is only one other type that is used as id (String)
    return ""


def upgrade_enterprise_dbs(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    for table_name in all_tables:
        table = Table(table_name, meta, autoload=True)

        new_deleted = Column('new_deleted', table.c.id.type,
                             default=get_default_deleted_value(table))
        new_deleted.create(table, populate_default=True)

        table.update().\
                where(table.c.deleted == True).\
                values(new_deleted=table.c.id).\
                execute()
        table.c.deleted.drop()
        table.c.new_deleted.alter(name="deleted")


def upgrade(migrate_engine):
    if migrate_engine.name != "sqlite":
        return upgrade_enterprise_dbs(migrate_engine)

    # NOTE(boris-42): sqlaclhemy-migrate can't drop column with check
    #                 constraints in sqlite DB and our `deleted` column has
    #                 2 check constraints. So there is only one way to remove
    #                 these constraints:
    #                 1) Create new table with the same columns, constraints
    #                 and indexes. (except deleted column).
    #                 2) Copy all data from old to new table.
    #                 3) Drop old table.
    #                 4) Rename new table to old table name.
    insp = reflection.Inspector.from_engine(migrate_engine)
    meta = MetaData()
    meta.bind = migrate_engine

    for table_name in all_tables:
        table = Table(table_name, meta, autoload=True)
        default_deleted_value = get_default_deleted_value(table)

        columns = []
        for column in table.columns:
            column_copy = None
            if column.name != "deleted":
                # NOTE(boris-42): BigInteger is not supported by sqlite, so
                #                 after copy it will have NullType, other
                #                 types that are used in Nova are supported by
                #                 sqlite.
                if isinstance(column.type, NullType):
                    column_copy = Column(column.name, BigInteger(), default=0)
                else:
                    column_copy = column.copy()
            else:
                column_copy = Column('deleted', table.c.id.type,
                                     default=default_deleted_value)
            columns.append(column_copy)

        def is_deleted_column_constraint(constraint):
            # NOTE(boris-42): There is no other way to check is CheckConstraint
            #                 associated with deleted column.
            if not isinstance(constraint, CheckConstraint):
                return False
            sqltext = str(constraint.sqltext)
            return (sqltext.endswith("deleted in (0, 1)") or
                    sqltext.endswith("deleted IN (:deleted_1, :deleted_2)"))

        constraints = []
        for constraint in table.constraints:
            if not is_deleted_column_constraint(constraint):
                constraints.append(constraint.copy())

        new_table = Table(table_name + "__tmp__", meta,
                          *(columns + constraints))
        new_table.create()

        indexes = []
        for index in insp.get_indexes(table_name):
            column_names = [new_table.c[c] for c in index['column_names']]
            indexes.append(Index(index["name"],
                                 *column_names,
                                 unique=index["unique"]))

        ins = InsertFromSelect(new_table, table.select())
        migrate_engine.execute(ins)

        table.drop()
        [index.create(migrate_engine) for index in indexes]

        new_table.rename(table_name)
        new_table.update().\
            where(new_table.c.deleted == True).\
            values(deleted=new_table.c.id).\
            execute()

        # NOTE(boris-42): Fix value of deleted column: False -> "" or 0.
        new_table.update().\
            where(new_table.c.deleted == False).\
            values(deleted=default_deleted_value).\
            execute()


def downgrade_enterprise_dbs(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    for table_name in all_tables:
        table = Table(table_name, meta, autoload=True)

        old_deleted = Column('old_deleted', Boolean, default=False)
        old_deleted.create(table, populate_default=False)

        table.update().\
                where(table.c.deleted == table.c.id).\
                values(old_deleted=True).\
                execute()

        table.c.deleted.drop()
        table.c.old_deleted.alter(name="deleted")


def downgrade(migrate_engine):
    if migrate_engine.name != "sqlite":
        return downgrade_enterprise_dbs(migrate_engine)

    insp = reflection.Inspector.from_engine(migrate_engine)
    meta = MetaData()
    meta.bind = migrate_engine

    for table_name in all_tables:
        table = Table(table_name, meta, autoload=True)

        columns = []
        for column in table.columns:
            column_copy = None
            if column.name != "deleted":
                if isinstance(column.type, NullType):
                    column_copy = Column(column.name, BigInteger(), default=0)
                else:
                    column_copy = column.copy()
            else:
                column_copy = Column('deleted', Boolean, default=0)
            columns.append(column_copy)

        constraints = [constraint.copy() for constraint in table.constraints]

        new_table = Table(table_name + "__tmp__", meta,
                          *(columns + constraints))
        new_table.create()

        indexes = []
        for index in insp.get_indexes(table_name):
            column_names = [new_table.c[c] for c in index['column_names']]
            indexes.append(Index(index["name"],
                                 *column_names,
                                 unique=index["unique"]))

        c_select = []
        for c in table.c:
            if c.name != "deleted":
                c_select.append(c)
            else:
                c_select.append(table.c.deleted == table.c.id)

        ins = InsertFromSelect(new_table, select(c_select))
        migrate_engine.execute(ins)

        table.drop()
        [index.create(migrate_engine) for index in indexes]

        new_table.rename(table_name)
        new_table.update().\
            where(new_table.c.deleted == new_table.c.id).\
            values(deleted=True).\
            execute()
