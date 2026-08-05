"""Tests for passing data between tasks in workflows."""

import polars as pl
import pytest

from avalanche.dag import dest, source, step, workflow
from avalanche.executor import LocalExecutor, RayExecutor


class TestDataPassingBetweenTasks:
    """Test passing results from one task to another."""

    def test_simple_data_passing_local(self):
        """Test passing data between tasks with LocalExecutor."""

        @source
        def load():
            return [1, 2, 3, 4, 5]

        @step
        def double(data):
            return [x * 2 for x in data]

        @dest
        def save(data):
            return f"saved_{len(data)}_items"

        @workflow
        def my_workflow():
            data = load()
            doubled = double(data)  # Pass result as arg
            result = save(doubled)  # Pass result as arg
            return data, doubled, result  # Return what we want to verify

        p = my_workflow()
        executor = LocalExecutor()
        load_result, double_result, save_result = p.run(executor=executor).result()

        # Verify results passed through chain
        assert load_result == [1, 2, 3, 4, 5]
        assert double_result == [2, 4, 6, 8, 10]
        assert save_result == "saved_5_items"

    def test_dataframe_passing_local(self):
        """Test passing Polars DataFrames between tasks."""

        @source
        def extract():
            return pl.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})

        @step
        def add_column(df):
            return df.with_columns((pl.col("value") * 2).alias("doubled"))

        @step
        def filter_rows(df):
            return df.filter(pl.col("id") > 1)

        @dest
        def count_rows(df):
            return len(df)

        @workflow
        def df_workflow():
            df1 = extract()
            df2 = add_column(df1)  # Pass DataFrame
            df3 = filter_rows(df2)  # Pass DataFrame
            count = count_rows(df3)  # Pass DataFrame
            return df1, df2, df3, count

        p = df_workflow()
        executor = LocalExecutor()
        df1, df2, df3, count = p.run(executor=executor).result()

        # Verify DataFrame passed through
        assert isinstance(df1, pl.DataFrame)
        assert len(df1) == 3

        assert isinstance(df2, pl.DataFrame)
        assert "doubled" in df2.columns

        assert isinstance(df3, pl.DataFrame)
        assert len(df3) == 2  # Filtered id > 1

        assert count == 2

    def test_multiple_args_passing(self):
        """Test passing multiple results to a single task."""

        @source
        def load_a():
            return [1, 2, 3]

        @source
        def load_b():
            return [4, 5, 6]

        @step
        def merge(data_a, data_b):
            return data_a + data_b

        @workflow
        def multi_source():
            a = load_a()
            b = load_b()
            merged = merge(a, b)  # Pass both results
            return a, b, merged

        p = multi_source()
        executor = LocalExecutor()
        a_result, b_result, merged_result = p.run(executor=executor).result()

        assert a_result == [1, 2, 3]
        assert b_result == [4, 5, 6]
        assert merged_result == [1, 2, 3, 4, 5, 6]

    def test_keyword_only_future_wiring_skips_implicit_binding(self):
        """Futures passed as kwargs must not be re-bound implicitly by position."""

        @source
        def load_a():
            return [1, 2, 3]

        @source
        def load_b():
            return [4, 5, 6]

        @step
        def merge(data_a, data_b):
            return data_a + data_b

        @workflow
        def keyword_wired():
            a = load_a()
            b = load_b()
            # Both upstream futures arrive as keyword arguments only.
            return merge(data_a=a, data_b=b)

        result = keyword_wired().run(executor=LocalExecutor()).result()

        assert result == [1, 2, 3, 4, 5, 6]

    def test_partial_keyword_future_wiring_with_plain_kwarg(self):
        """A future kwarg and a plain-value kwarg coexist without double-binding."""

        @source
        def load_a():
            return [1, 2, 3]

        @step
        def merge(data_a, suffix):
            return data_a + suffix

        @workflow
        def mixed_wired():
            return merge(data_a=load_a(), suffix=[9])

        result = mixed_wired().run(executor=LocalExecutor()).result()

        assert result == [1, 2, 3, 9]

    def test_tuple_unpacking_local(self):
        """Test unpacking tuple results from tasks."""

        @source(num_returns=2)
        def load_pair():
            return ([1, 2, 3], [4, 5, 6])

        @step
        def process_first(data):
            return [x * 2 for x in data]

        @step
        def process_second(data):
            return [x * 3 for x in data]

        @dest
        def combine(data_a, data_b):
            return {"first": data_a, "second": data_b}

        @workflow
        def tuple_workflow():
            pair = load_pair()
            a = pair[0]  # Extract first element from tuple
            b = pair[1]  # Extract second element from tuple
            processed_a = process_first(a)
            processed_b = process_second(b)
            result = combine(processed_a, processed_b)
            return pair, processed_a, processed_b, result

        p = tuple_workflow()
        executor = LocalExecutor()
        pair_result, proc_a, proc_b, combined = p.run(executor=executor).result()

        # Verify tuple was unpacked correctly
        assert pair_result == ([1, 2, 3], [4, 5, 6])
        assert proc_a == [2, 4, 6]
        assert proc_b == [12, 15, 18]
        assert combined == {"first": [2, 4, 6], "second": [12, 15, 18]}

    def test_multiple_return_values(self):
        """Test task returning multiple values and accessing them."""

        @source
        def load_pair():
            return {"first": [1, 2, 3], "second": [4, 5, 6]}

        @step
        def process_dict(data_dict):
            return {
                "first": [x * 2 for x in data_dict["first"]],
                "second": [x * 3 for x in data_dict["second"]],
            }

        @dest
        def summarize(data):
            return {"count": len(data["first"]) + len(data["second"])}

        @workflow
        def dict_workflow():
            pair = load_pair()
            processed = process_dict(pair)  # Pass dict
            summary = summarize(processed)
            return pair, processed, summary

        p = dict_workflow()
        executor = LocalExecutor()
        pair_result, processed_result, summary_result = p.run(executor=executor).result()

        assert pair_result == {"first": [1, 2, 3], "second": [4, 5, 6]}
        assert processed_result == {"first": [2, 4, 6], "second": [12, 15, 18]}
        assert summary_result == {"count": 6}

    @pytest.mark.ray
    def test_dataframe_passing_with_ray(self):
        """Test passing DataFrames through Ray executor."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
        )

        try:

            @source
            def extract():
                return pl.DataFrame({"id": [1, 2, 3, 4, 5], "value": [10, 20, 30, 40, 50]})

            @step
            def transform_df(df):
                return df.with_columns((pl.col("value") * 2).alias("doubled"))

            @dest
            def aggregate(df):
                return {"count": len(df), "total": df["value"].sum()}

            @workflow
            def ray_df_workflow():
                df1 = extract()
                df2 = transform_df(df1)  # Pass DataFrame through Ray
                agg = aggregate(df2)
                return agg  # Just return final result

            p = ray_df_workflow()
            executor = RayExecutor()
            result = p.run(executor=executor).result()

            # Verify DataFrames passed through Ray correctly
            assert result == {"count": 5, "total": 150}

            print("✓ DataFrames passed through Ray successfully!")
            print(f"  Results: {result}")

        finally:
            ray.shutdown()

    @pytest.mark.ray
    def test_tuple_unpacking_with_ray(self):
        """Test tuple unpacking through Ray executor."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
        )

        try:

            @source(num_returns=2)
            def load_datasets():
                return (
                    pl.DataFrame({"id": [1, 2], "value": [10, 20]}),
                    pl.DataFrame({"id": [3, 4], "value": [30, 40]}),
                )

            @step
            def sum_first(df):
                return df["value"].sum()

            @step
            def sum_second(df):
                return df["value"].sum()

            @dest
            def combine_sums(sum_a, sum_b):
                return sum_a + sum_b

            @workflow
            def tuple_ray_workflow():
                datasets = load_datasets()
                df_a = datasets[0]  # Extract first DataFrame from tuple
                df_b = datasets[1]  # Extract second DataFrame from tuple
                total_a = sum_first(df_a)
                total_b = sum_second(df_b)
                combined = combine_sums(total_a, total_b)
                return combined  # Just return final result

            p = tuple_ray_workflow()
            executor = RayExecutor()
            result = p.run(executor=executor).result()

            # Verify tuple unpacking worked through Ray
            assert result == 100  # 30 + 70

            print("✓ Tuple unpacking worked through Ray!")
            print(f"  Unpacked DataFrames and computed: {result}")

        finally:
            ray.shutdown()
